#!/usr/bin/env python

import numpy as np, pandas as pd
import boto3, re, time, typing, socket, paramiko, os, pysftp, collections, json, fire, shlex
from typing import Callable,List,Dict,Tuple,Union,Optional,Iterable
from pathlib import Path
from dateutil.parser import parse
from pkg_resources import resource_filename
from pdb import set_trace

__all__ = ['EC2', 'main']

here = os.path.abspath(os.path.dirname(__file__)) + '/'

def _get_dict(l): return collections.defaultdict(str, {o['Key']:o['Value'] for o in l})
def _make_dict(d:Dict):   return [{'Key':k, 'Value':  v } for k,v in (d or {}).items()]

boto3.resources.base.ServiceResource.name = property(
    lambda o: _get_dict(o.tags)['Name'])

def _boto3_repr(self):
    clname =  self.__class__.__name__
    if clname == 'ec2.Instance':
        return f'{self.name} ({self.id} {self.instance_type} {self.state["Name"]}): {self.public_ip_address or "No public IP"}'
    elif clname == 'ec2.Image':
        root_dev = [o for o in self.block_device_mappings if self.root_device_name == o['DeviceName']]
        return f'{self.name} ({self.id}): {root_dev[0]["Ebs"]["VolumeSize"]}GB'
    else:
        identifiers = [f'{ident}={repr(getattr(self, ident))}' for ident in self.meta.identifiers]
        return f"{self.__class__.__name__}({', '.join(identifiers)})"
boto3.resources.base.ServiceResource.__repr__ = _boto3_repr

_in_notebook = False
try:
    from ipykernel.kernelapp import IPKernelApp
    _in_notebook = IPKernelApp.initialized()
except: pass

def listify(p=None, q=None):
    "Make `p` listy and the same length as `q`."
    if p is None: p=[]
    elif isinstance(p, str):          p=[p]
    elif not isinstance(p, Iterable): p=[p]
    n = q if type(q)==int else len(p) if q is None else len(q)
    if len(p)==1: p = p * n
    assert len(p)==n, f'List len mismatch ({len(p)} vs {n})'
    return list(p)

def make_filter(d:Dict):
    d = {k.replace('_','-'):v for k,v in d.items()}
    return {'Filters': [{'Name':k, 'Values':listify(v)} for k,v in (d or {}).items()]}

def result(r):
    if isinstance(r, typing.List): r = r[0]
    k = [o for o in r.keys() if r !='ResponseMetadata']
    if not k: return None
    return r[k[0]]

def get_regions():
    endpoint_file = resource_filename('botocore', 'data/endpoints.json')
    with open(endpoint_file, 'r') as f: a = json.load(f)
    return {k:v['description'] for k,v in a['partitions'][0]['regions'].items()}

def get_insttypes():
    "Dict of instance types (eg 'p3.8xlarge') for each instance category (eg 'p3')"
    s = [o.strip() for o in open(here+'insttypes.txt').readlines()]
    d = collections.defaultdict(list)
    for l in s: d[l[:2]].append(l.strip())
    return d


class EC2():
    def __init__(self, region:str=None):
        if region is not None: boto3.setup_default_session(region_name=region)
        self._ec2 = boto3.client('ec2')
        self._ec2r = boto3.resource('ec2')
        self.insttypes = get_insttypes()

    def _resources(self, coll_name, **filters):
        coll = getattr(self._ec2r,coll_name)
        return coll.filter(**make_filter(filters))

    def resource(self, coll_name, **filters):
        "The first resource from collection `coll_name` matching `filters`"
        try: return next(iter(self._resources(coll_name, **filters)))
        except StopIteration: raise KeyError(f'Resource not found: {coll_name}; {filters}') from None

    def region(self, region:str):
        "Get first region containing substring `region`"
        regions = get_regions()
        if region in regions: return region
        return next(r for r,n in regions.items() if region in n)

    def _describe(self, f:str, d:Dict=None, **kwargs):
        "Calls `describe_{f}` with filter `d` and `kwargs`"
        return result(getattr(self._ec2, 'describe_'+f)(**make_filter(d), **kwargs))

    def instances(self):
        "Print all non-terminated instances"
        states = ['pending', 'running', 'stopping', 'stopped']
        for o in (self._resources('instances', instance_state_name=states)): print(o)

    def _price_hist(self, insttype):
        types = self.insttypes[insttype]
        prices = self._ec2.describe_spot_price_history(InstanceTypes=types, ProductDescriptions=["Linux/UNIX"])
        df = pd.DataFrame(prices['SpotPriceHistory'])
        df["SpotPrice"] = df.SpotPrice.astype(float)
        return df.pivot_table(values='SpotPrice', index='Timestamp', columns='InstanceType', aggfunc='min'
                             ).resample('1D').min().reindex(columns=types).tail(50)

    def price_hist(self, insttype):
        pv = self._price_hist(insttype)
        res = pv.tail(3).T
        if _in_notebook:
            pv.plot()
            return res
        print(res)

    def price_demand(self, insttype):
        "On demand prices for `insttype` (currently only shows us-east-1 prices)"
        prices = dict(pd.read_csv(here+'prices.csv').values)
        return [(o,round(prices[o],3)) for o in self.insttypes[insttype]]

    def waitfor(self, which, timeout, **filt):
        waiter = self._ec2.get_waiter(which)
        waiter.config.max_attempts = timeout//15
        waiter.wait(**filt)

    def get_secgroup(self, secgroupname):
        "Get security group id from `secgroupname`, creating it if needed (with just port 22 ingress)"
        try: secgroup = self.resource('security_groups', group_name=secgroupname)
        except KeyError:
            vpc = self.resource('vpcs', isDefault='true')
            secgroup = self._ec2r.create_security_group(GroupName=secgroupname, Description=secgroupname, VpcId=vpc.id)
            secgroup.authorize_ingress(IpPermissions=[{
                'IpRanges': [{'CidrIp': '0.0.0.0/0'}], 'FromPort': 22, 'ToPort': 22,
                'IpProtocol': 'tcp'}] )
        return secgroup

    def get_amis(self, description=None, owner=None, filt_func=None):
        """Return all AMIs with `owner` (or private AMIs if None), optionally matching `description` and `filt_func`.
        Sorted by `creation_date` descending"""
        if filt_func is None: filt_func=lambda o:True
        filt = dict(architecture='x86_64', virtualization_type='hvm', state='available', root_device_type='ebs')
        if owner is None: filt['is_public'] = 'false'
        else:             filt['owner-id'] = owner
        if description is not None: filt['description'] = description
        print(filt)
        amis = self._resources('images', **filt)
        amis = [o for o in amis if filt_func(o)]
        return sorted(amis, key=lambda o: parse(o.creation_date), reverse=True)

    def amis(self, description=None, owner=None, filt_func=None):
        """Return all AMIs with `owner` (or private AMIs if None), optionally matching `description` and `filt_func`.
        Sorted by `creation_date` descending"""
        for ami in self.get_amis(description, owner, filt_func): print(ami)

    def get_ami(self, aminame=None):
        "Look up `aminame` if provided, otherwise find latest Ubuntu 18.04 image"
        # If passed a valid AMI id, just return it
        try: return self.resource('images', image_id=aminame)
        except KeyError: pass
        if aminame: return self.resource('images', name=aminame, is_public='false')
        amis = self.get_amis('Canonical, Ubuntu, 18.04 LTS*',owner='099720109477',
                              filt_func=lambda o: not re.search(r'UNSUPPORTED|minimal', o.description))
        assert amis, 'AMI not found'
        return amis[0]

    def ami(self, aminame=None): print(self.get_ami(aminame))

    def change_type(self, name, insttype):
        inst = self.get_instance(name)
        inst.modify_attribute(Attribute='instanceType', Value=insttype)

    def freeze(self, name):
        inst = self.get_instance(name)
        return self._ec2.create_image(InstanceId=inst.id, Name=name)['ImageId']

    def _launch_spec(self, ami, keyname, disksize, instancetype, secgroupid, iops=None):
        assert self._describe('key_pairs', {'key-name':keyname}), 'default key not found'
        ami = self.get_ami(ami)
        ebs = ({'VolumeSize': disksize, 'VolumeType': 'io1', 'Iops': 6000 }
                 if iops else {'VolumeSize': disksize, 'VolumeType': 'gp2'})
        return { 'ImageId': ami.id, 'InstanceType': instancetype,
            'SecurityGroupIds': [secgroupid], 'KeyName': keyname,
            "BlockDeviceMappings": [{ "DeviceName": "/dev/sda1", "Ebs": ebs, }] }

    def request_spot(self, ami, keyname, disksize, instancetype, secgroupid, iops=None):
        spec = self._launch_spec(ami, keyname, disksize, instancetype, secgroupid, iops)
        sr = result(self._ec2.request_spot_instances(LaunchSpecification=spec))
        assert len(sr)==1, 'spot request failed'
        srid = sr[0]['SpotInstanceRequestId']
        self.waitfor('spot_instance_request_fulfilled', 180, SpotInstanceRequestIds=[srid])
        time.sleep(5)
        instid = self._describe('spot_instance_requests', {'spot-instance-request-id':srid})[0]['InstanceId']
        return self._ec2r.Instance(instid)

    def request_demand(self, ami, keyname, disksize, instancetype, secgroupid, iops=None):
        spec = self._launch_spec(ami, keyname, disksize, instancetype, secgroupid, iops)
        return self._ec2r.create_instances(MinCount=1, MaxCount=1, **spec)[0]

    def _wait_ssh(self, inst):
        self.waitfor('instance_running', 180, InstanceIds=[inst.id])
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            for i in range(720//5):
                try:
                    s.connect((inst.public_ip_address, 22))
                    time.sleep(1)
                    return inst
                except (ConnectionRefusedError,BlockingIOError): time.sleep(5)

    def get_launch(self, name, ami, disksize, instancetype, keyname:str='default', secgroupname:str='ssh', iops:int=None, spot:bool=False):
        "Creates new instance `name` and returns `Instance` object"
        insts = self._describe('instances', {'tag:Name':name})
        assert not insts, 'name already exists'
        secgroupid = self.get_secgroup(secgroupname).id
        if spot: inst = self.request_spot  (ami, keyname, disksize, instancetype, secgroupid, iops)
        else   : inst = self.request_demand(ami, keyname, disksize, instancetype, secgroupid, iops)
        inst.create_tags(Tags=_make_dict({'Name':name}))
        self._wait_ssh(inst)

    def launch(self, name, ami, disksize, instancetype, keyname:str='default', secgroupname:str='ssh', iops:int=None, spot:bool=False):
        print(self.get_launch(name, ami, disksize, instancetype, keyname, secgroupname, iops, spot))

    def get_instance(self, name:str):
        "Get `Instance` object for `name`"
        filt = make_filter({'tag:Name':name})
        return next(iter(self._ec2r.instances.filter(**filt)))

    def instance(self, name:str): print(self.get_instance(name))

    def get_start(self, name):
        "Starts instance `name` and returns `Instance` object"
        inst = self.get_instance(name)
        inst.start()
        return self._wait_ssh(inst)

    def start(self, name): print(self.get_start(name))

    def connect(self, name, ports=None):
        """Replace python process with an ssh process connected to instance `name`;
        use `user@name` otherwise defaults to user 'ubuntu'. `ports` (int or list) creates tunnels"""
        if '@' in name: user,name = name.split('@')
        else: user = 'ubuntu'
        inst = self.get_instance(name)
        tunnel = []
        if ports is not None: tunnel = [f'-L {o}:localhost:{o}' for o in listify(ports)]
        os.execvp('ssh', ['ssh', f'{user}@{inst.public_ip_address}', *tunnel])

    def ssh(self, name, user='ubuntu', keyfile='~/.ssh/id_rsa'):
        "Return a paramiko ssh connection objected connected to instance `name`"
        inst = self.get_instance(name)
        keyfile = os.path.expanduser(keyfile)
        key = paramiko.RSAKey.from_private_key_file(keyfile)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=inst.public_ip_address, username=user, pkey=key)
        client.raise_stderr = True
        client.launch_tmux()
        return client

def _run_ssh(ssh, cmd, pty=False):
    stdin, stdout, stderr = ssh.exec_command(cmd, get_pty=pty)
    stdout_str = stdout.read().decode()
    stderr_str = stderr.read().decode()
    if stdout.channel.recv_exit_status() != 0: raise Exception(stderr_str)
    if ssh.raise_stderr:
        if stderr_str: raise Exception(stderr_str)
        return stdout_str
    return stdout_str,stderr_str

def _check_ssh(ssh): assert ssh.run('echo hi')[0] == 'hi\n'

def _launch_tmux(ssh):
    try: ssh.run('tmux ls')
    except: ssh.run('tmux new -n 0 -d', pty=True)
    return ssh

def _send_tmux(ssh, cmd):
    ssh.run(f'tmux send-keys -l {shlex.quote(cmd)}')
    ssh.run(f'tmux send-keys Enter')

paramiko.SSHClient.run = _run_ssh
paramiko.SSHClient.check = _check_ssh
paramiko.SSHClient.send = _send_tmux
paramiko.SSHClient.launch_tmux = _launch_tmux

def _pysftp_init(self, transport):
    self._sftp_live = True
    self._transport = transport
    self._sftp = paramiko.SFTPClient.from_transport(self._transport)

def _put_dir(sftp, fr, to):
    sftp.makedirs(to)
    sftp.put_d(os.path.expanduser(fr), to)

def _put_key(sftp, name):
    sftp.put(os.path.expanduser(f'~/.ssh/{name}'), f'.ssh/{name}')
    sftp.chmod(f'.ssh/{name}', 400)

pysftp.Connection.__init__ = _pysftp_init
pysftp.Connection.put_dir = _put_dir
pysftp.Connection.put_key = _put_key

def main(): fire.Fire(EC2)
if __name__ == '__main__': main()
