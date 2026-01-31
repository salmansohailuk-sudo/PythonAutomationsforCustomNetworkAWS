# ====================================================
# Dependency Auto-Check
# ====================================================
import sys, subprocess
for pkg in ["boto3"]:
    try:
        __import__(pkg)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

# ====================================================
# Imports
# ====================================================
import boto3
import botocore
import time
import os
import stat
import logging

# ====================================================
# Logging Setup
# ====================================================
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/infra.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger()

# ====================================================
# Config
# ====================================================
REGION = "us-east-1"
KEY_NAME = "salman-key"
VPC_NAME = "salman-vpc"
AMI_ID = "ami-0532be01f26a3de55"
INSTANCE_TYPE = "t2.micro"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Store key outside .venv to avoid Windows permission issues
KEY_DIR = os.path.join(BASE_DIR, "Key")
KEY_PATH = os.path.join(KEY_DIR, f"{KEY_NAME}.pem")

# ====================================================
# AWS Clients
# ====================================================
ec2 = boto3.client("ec2", region_name=REGION)
elbv2 = boto3.client("elbv2", region_name=REGION)

# ====================================================
# Helper Functions
# ====================================================
def recreate_keypair():
    """Delete existing key pair if exists, create new one (Windows-safe)."""
    os.makedirs(KEY_DIR, exist_ok=True)
    try:
        ec2.describe_key_pairs(KeyNames=[KEY_NAME])
        log.info(f"Deleting existing key pair: {KEY_NAME}")
        ec2.delete_key_pair(KeyName=KEY_NAME)
        if os.path.exists(KEY_PATH):
            os.chmod(KEY_PATH, stat.S_IWRITE)
            os.remove(KEY_PATH)
    except botocore.exceptions.ClientError:
        log.info(f"No existing key pair named {KEY_NAME}")

    key = ec2.create_key_pair(KeyName=KEY_NAME)
    with open(KEY_PATH, "w") as f:
        f.write(key["KeyMaterial"])
    try:
        os.chmod(KEY_PATH, stat.S_IRUSR)
    except Exception:
        pass
    log.info(f"New key pair created: {KEY_NAME} at {KEY_PATH}")

def get_or_create_vpc():
    vpcs = ec2.describe_vpcs(Filters=[{"Name":"tag:Name","Values":[VPC_NAME]}])["Vpcs"]
    if vpcs:
        log.info(f"VPC {VPC_NAME} exists: {vpcs[0]['VpcId']}")
        return vpcs[0]["VpcId"]

    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    ec2.create_tags(Resources=[vpc], Tags=[{"Key":"Name","Value":VPC_NAME}])
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsSupport={"Value":True})
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsHostnames={"Value":True})
    log.info(f"VPC {VPC_NAME} created: {vpc}")
    return vpc

def get_or_create_subnet(vpc, cidr, az, name, public=False):
    subnets = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc]},
            {"Name": "cidr-block", "Values": [cidr]}
        ]
    )["Subnets"]
    if subnets:
        log.info(f"Subnet {name} exists: {subnets[0]['SubnetId']}")
        return subnets[0]["SubnetId"]

    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock=cidr, AvailabilityZone=az)["Subnet"]["SubnetId"]
    ec2.create_tags(Resources=[subnet], Tags=[{"Key":"Name","Value":name}])
    if public:
        ec2.modify_subnet_attribute(SubnetId=subnet, MapPublicIpOnLaunch={"Value":True})
    log.info(f"Subnet {name} created: {subnet}")
    return subnet

def get_or_create_igw(vpc):
    igws = ec2.describe_internet_gateways(
        Filters=[{"Name":"attachment.vpc-id","Values":[vpc]}]
    )["InternetGateways"]
    if igws:
        log.info(f"Internet Gateway exists: {igws[0]['InternetGatewayId']}")
        return igws[0]["InternetGatewayId"]

    igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
    log.info(f"Internet Gateway created: {igw}")
    return igw

def create_nat(public_subnet):
    eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat = ec2.create_nat_gateway(SubnetId=public_subnet, AllocationId=eip)["NatGateway"]["NatGatewayId"]

    log.info("Waiting for NAT Gateway to become available...")
    while True:
        state = ec2.describe_nat_gateways(NatGatewayIds=[nat])["NatGateways"][0]["State"]
        if state == "available":
            break
        time.sleep(10)
    log.info(f"NAT Gateway ready: {nat}")
    return nat

def get_or_create_sg(vpc, name, description):
    sgs = ec2.describe_security_groups(
        Filters=[{"Name":"group-name","Values":[name]}, {"Name":"vpc-id","Values":[vpc]}]
    )["SecurityGroups"]
    if sgs:
        log.info(f"Security Group {name} exists: {sgs[0]['GroupId']}")
        return sgs[0]["GroupId"]

    sg = ec2.create_security_group(GroupName=name, Description=description, VpcId=vpc)["GroupId"]
    log.info(f"Security Group {name} created: {sg}")
    return sg

def launch_instance(name, subnet, sg, user_data, public_ip=False):
    instances = ec2.describe_instances(
        Filters=[{"Name":"tag:Name","Values":[name]}]
    )["Reservations"]
    if instances:
        iid = instances[0]["Instances"][0]["InstanceId"]
        log.info(f"EC2 instance {name} already exists: {iid}")
        return iid

    resp = ec2.run_instances(
        ImageId=AMI_ID,
        InstanceType=INSTANCE_TYPE,
        KeyName=KEY_NAME,
        MinCount=1,
        MaxCount=1,
        NetworkInterfaces=[{
            "SubnetId": subnet,
            "DeviceIndex": 0,
            "AssociatePublicIpAddress": public_ip,
            "Groups": [sg]
        }],
        TagSpecifications=[{
            "ResourceType":"instance",
            "Tags":[{"Key":"Name","Value":name}]
        }],
        UserData=user_data
    )
    iid = resp["Instances"][0]["InstanceId"]
    log.info(f"EC2 launched: {iid}")
    return iid

# ====================================================
# ALB + Target Group
# ====================================================
def create_alb(name, subnets, sg, vpc):
    lbs = elbv2.describe_load_balancers()["LoadBalancers"]
    for lb in lbs:
        if lb["LoadBalancerName"] == name:
            alb_arn = lb['LoadBalancerArn']
            log.info(f"ALB {name} exists: {alb_arn}")
            elbv2.add_tags(ResourceArns=[alb_arn], Tags=[{"Key":"Name","Value":name}])
            return alb_arn

    alb = elbv2.create_load_balancer(
        Name=name,
        Subnets=subnets,
        SecurityGroups=[sg],
        Scheme="internet-facing",
        Type="application"
    )["LoadBalancers"][0]
    alb_arn = alb['LoadBalancerArn']
    elbv2.add_tags(ResourceArns=[alb_arn], Tags=[{"Key":"Name","Value":name}])
    log.info(f"ALB created: {alb_arn}")
    return alb_arn

def create_target_group(name, vpc):
    tgs = elbv2.describe_target_groups()["TargetGroups"]
    for tg in tgs:
        if tg["TargetGroupName"] == name:
            tg_arn = tg["TargetGroupArn"]
            log.info(f"Target group {name} exists: {tg_arn}")
            elbv2.add_tags(ResourceArns=[tg_arn], Tags=[{"Key":"Name","Value":name}])
            return tg_arn

    tg = elbv2.create_target_group(
        Name=name,
        Protocol="HTTP",
        Port=80,
        VpcId=vpc,
        HealthCheckProtocol="HTTP",
        HealthCheckPort="80",
        HealthCheckPath="/",
        HealthCheckIntervalSeconds=15,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=2,
        TargetType="instance"
    )["TargetGroups"][0]
    tg_arn = tg["TargetGroupArn"]
    elbv2.add_tags(ResourceArns=[tg_arn], Tags=[{"Key":"Name","Value":name}])
    log.info(f"Target group created: {tg_arn}")
    return tg_arn

def create_listener(alb, tg):
    listeners = elbv2.describe_listeners(LoadBalancerArn=alb)["Listeners"]
    if listeners:
        log.info(f"Listener already exists for ALB: {alb}")
        return

    listener = elbv2.create_listener(
        LoadBalancerArn=alb,
        Protocol="HTTP",
        Port=80,
        DefaultActions=[{"Type":"forward","TargetGroupArn":tg}]
    )["Listeners"][0]
    listener_arn = listener['ListenerArn']
    elbv2.add_tags(ResourceArns=[listener_arn], Tags=[{"Key":"Name","Value":"http-listener"}])
    log.info(f"Listener created and tagged: {listener_arn}")

# ====================================================
# Route Tables
# ====================================================
def create_route_tables(vpc, public_subnets, private_subnets, igw, nat):
    # Public Route Table
    public_rt = ec2.create_route_table(VpcId=vpc)["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=public_rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)
    for subnet in public_subnets:
        ec2.associate_route_table(SubnetId=subnet, RouteTableId=public_rt)
    log.info(f"Public route table created and associated: {public_rt}")

    # Private Route Table
    private_rt = ec2.create_route_table(VpcId=vpc)["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=private_rt, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat)
    for subnet in private_subnets:
        ec2.associate_route_table(SubnetId=subnet, RouteTableId=private_rt)
    log.info(f"Private route table created and associated: {private_rt}")

# ====================================================
# User Data
# ====================================================
USER_DATA = """#!/bin/bash
dnf install -y httpd
systemctl enable httpd
systemctl start httpd
echo "<h1>Healthy from $(hostname)</h1>" > /var/www/html/index.html
"""

# ====================================================
# EXECUTION
# ====================================================
recreate_keypair()
vpc = get_or_create_vpc()

pub1 = get_or_create_subnet(vpc,"10.0.1.0/24","us-east-1a","public-1",True)
pub2 = get_or_create_subnet(vpc,"10.0.2.0/24","us-east-1b","public-2",True)
priv1 = get_or_create_subnet(vpc,"10.0.3.0/24","us-east-1a","private-1")
priv2 = get_or_create_subnet(vpc,"10.0.4.0/24","us-east-1b","private-2")

igw = get_or_create_igw(vpc)
nat = create_nat(pub1)

create_route_tables(vpc, [pub1,pub2], [priv1,priv2], igw, nat)

public_sg = get_or_create_sg(vpc,"public-sg","public")
private_sg = get_or_create_sg(vpc,"private-sg","private")

try:
    ec2.authorize_security_group_ingress(
        GroupId=private_sg,
        IpPermissions=[{
            "IpProtocol":"tcp","FromPort":80,"ToPort":80,
            "IpRanges":[{"CidrIp":"10.0.0.0/16"}]
        }]
    )
except botocore.exceptions.ClientError as e:
    if "InvalidPermission.Duplicate" in str(e):
        log.info("Ingress rule already exists")
    else:
        raise e

i1 = launch_instance("app-1", priv1, private_sg, USER_DATA)
i2 = launch_instance("app-2", priv2, private_sg, USER_DATA)

alb_arn = create_alb("salman-alb",[pub1,pub2],public_sg,vpc)
tg_arn = create_target_group("salman-tg",vpc)

elbv2.register_targets(
    TargetGroupArn=tg_arn,
    Targets=[{"Id":i1,"Port":80},{"Id":i2,"Port":80}]
)

create_listener(alb_arn, tg_arn)
log.info("✅ FULL VPC DEPLOYMENT WITH ALB + HEALTH CHECK + FRESH KEY COMPLETE")
