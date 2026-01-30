import boto3
import botocore
import time
import os
import stat

# ----------------------------------------------------
# Configuration
# ----------------------------------------------------
REGION = "us-east-1"
KEY_NAME = "salman-key"
VPC_NAME = "salman-vpc"
AMI_ID = "ami-0532be01f26a3de55"
INSTANCE_TYPE = "t2.micro"
RDS_PASSWORD = "Test12345"
S3_BUCKET = "salman-bucket-0987"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_DIR = os.path.join(BASE_DIR, "Key")
KEY_PATH = os.path.join(KEY_DIR, f"{KEY_NAME}.pem")
print("Key folder path:", KEY_DIR)

# AWS clients
ec2 = boto3.client("ec2", region_name=REGION)
rds = boto3.client("rds", region_name=REGION)
elbv2 = boto3.client("elbv2", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)

# ----------------------------------------------------
# Helper Functions
# ----------------------------------------------------
def wait(msg, seconds=5):
    print(msg)
    time.sleep(seconds)

def get_or_create_keypair(overwrite=True):
    os.makedirs(KEY_DIR, exist_ok=True)
    try:
        ec2.describe_key_pairs(KeyNames=[KEY_NAME])
        print(f"✔ Key pair '{KEY_NAME}' exists in AWS")
        if os.path.exists(KEY_PATH) and overwrite:
            print(f"⚠ Overwriting local key file {KEY_PATH}")
            with open(KEY_PATH, "w") as f:
                f.write("")  # placeholder, AWS doesn't allow re-download
            try:
                os.chmod(KEY_PATH, stat.S_IRUSR)
            except Exception:
                pass
        elif os.path.exists(KEY_PATH):
            print(f"✔ Key exists locally: {KEY_PATH}")
    except botocore.exceptions.ClientError as e:
        if "InvalidKeyPair.NotFound" in str(e):
            key = ec2.create_key_pair(KeyName=KEY_NAME)
            with open(KEY_PATH, "w") as f:
                f.write(key["KeyMaterial"])
            try:
                os.chmod(KEY_PATH, stat.S_IRUSR)
            except Exception:
                pass
            print(f"✔ Key pair created and saved to {KEY_PATH}")
        else:
            raise

def get_or_create_vpc():
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [VPC_NAME]}])["Vpcs"]
    if vpcs:
        print("✔ VPC exists")
        return vpcs[0]["VpcId"]
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    ec2.create_tags(Resources=[vpc], Tags=[{"Key": "Name", "Value": VPC_NAME}])
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsHostnames={"Value": True})
    print("✔ VPC created")
    return vpc

def get_or_create_subnet(vpc, cidr, name, public=False, az="us-east-1a"):
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}, {"Name": "cidr-block", "Values": [cidr]}])["Subnets"]
    if subnets:
        print(f"✔ Subnet {name} exists")
        return subnets[0]["SubnetId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock=cidr, AvailabilityZone=az)["Subnet"]["SubnetId"]
    ec2.create_tags(Resources=[subnet], Tags=[{"Key": "Name", "Value": name}])
    if public:
        ec2.modify_subnet_attribute(SubnetId=subnet, MapPublicIpOnLaunch={"Value": True})
    print(f"✔ Subnet {name} created")
    return subnet

def get_or_create_igw(vpc):
    igws = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc]}])["InternetGateways"]
    if igws:
        print("✔ Internet Gateway exists")
        return igws[0]["InternetGatewayId"]
    igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
    print("✔ Internet Gateway created and attached")
    return igw

def wait_for_nat(nat_id):
    while True:
        state = ec2.describe_nat_gateways(NatGatewayIds=[nat_id])["NatGateways"][0]["State"]
        if state == "available":
            print("✔ NAT Gateway available")
            return
        print("⏳ Waiting for NAT Gateway...")
        time.sleep(10)

def get_or_create_nat(public_subnet):
    nats = ec2.describe_nat_gateways(Filters=[{"Name": "subnet-id", "Values": [public_subnet]}])["NatGateways"]
    if nats:
        print("✔ NAT Gateway exists")
        return nats[0]["NatGatewayId"]
    eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat = ec2.create_nat_gateway(SubnetId=public_subnet, AllocationId=eip)["NatGateway"]["NatGatewayId"]
    wait_for_nat(nat)
    return nat

def get_or_create_route_table(vpc, name):
    rts = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc]}, {"Name": "tag:Name", "Values": [name]}])["RouteTables"]
    if rts:
        print(f"✔ Route table {name} exists")
        return rts[0]["RouteTableId"]
    rt = ec2.create_route_table(VpcId=vpc)["RouteTable"]["RouteTableId"]
    ec2.create_tags(Resources=[rt], Tags=[{"Key": "Name", "Value": name}])
    print(f"✔ Route table {name} created")
    return rt

def ensure_route(rt_id, destination, igw_id=None, nat_id=None):
    routes = ec2.describe_route_tables(RouteTableIds=[rt_id])["RouteTables"][0]["Routes"]
    for r in routes:
        if r.get("DestinationCidrBlock") == destination:
            return
    params = {"RouteTableId": rt_id, "DestinationCidrBlock": destination}
    if igw_id:
        params["GatewayId"] = igw_id
    if nat_id:
        params["NatGatewayId"] = nat_id
    ec2.create_route(**params)

def associate_rt(rt_id, subnet_id):
    associations = ec2.describe_route_tables(RouteTableIds=[rt_id])["RouteTables"][0]["Associations"]
    for a in associations:
        if a.get("SubnetId") == subnet_id:
            return
    ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)

def get_or_create_sg(name, desc, vpc, rules):
    sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name","Values":[name]},{"Name":"vpc-id","Values":[vpc]}])["SecurityGroups"]
    if sgs:
        print(f"✔ Security Group {name} exists")
        return sgs[0]["GroupId"]
    sg = ec2.create_security_group(GroupName=name, Description=desc, VpcId=vpc)["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=rules)
    print(f"✔ Security Group {name} created")
    return sg

# ----------------------------------------------------
# Launch EC2 Instances
# ----------------------------------------------------
def launch_instance(name, subnet_id, sg_id, user_data=""):
    resp = ec2.run_instances(
        ImageId=AMI_ID,
        InstanceType=INSTANCE_TYPE,
        KeyName=KEY_NAME,
        MaxCount=1,
        MinCount=1,
        NetworkInterfaces=[{"SubnetId":subnet_id,"DeviceIndex":0,"AssociatePublicIpAddress":False,"Groups":[sg_id]}],
        TagSpecifications=[{"ResourceType":"instance","Tags":[{"Key":"Name","Value":name}]}],
        UserData=user_data
    )
    instance_id = resp["Instances"][0]["InstanceId"]
    print(f"✔ EC2 {name} launched with ID {instance_id}")
    return instance_id

# ----------------------------------------------------
# ALB & Target Group
# ----------------------------------------------------
def get_or_create_alb(name, subnets, sg_id):
    try:
        lbs = elbv2.describe_load_balancers(Names=[name])["LoadBalancers"]
        print(f"✔ ALB {name} exists")
        return lbs[0]["LoadBalancerArn"]
    except elbv2.exceptions.LoadBalancerNotFoundException:
        alb = elbv2.create_load_balancer(
            Name=name,
            Subnets=subnets,
            SecurityGroups=[sg_id],
            Scheme="internet-facing",
            Type="application",
            IpAddressType="ipv4"
        )["LoadBalancers"][0]
        print(f"✔ ALB {name} created")
        return alb["LoadBalancerArn"]

def get_or_create_target_group(name, vpc):
    try:
        tgs = elbv2.describe_target_groups(Names=[name])["TargetGroups"]
        print(f"✔ Target Group {name} exists")
        return tgs[0]["TargetGroupArn"]
    except elbv2.exceptions.TargetGroupNotFoundException:
        tg = elbv2.create_target_group(
            Name=name,
            Protocol="HTTP",
            Port=80,
            VpcId=vpc,
            TargetType="instance"
        )["TargetGroups"][0]
        print(f"✔ Target Group {name} created")
        return tg["TargetGroupArn"]

def create_listener(alb_arn, tg_arn):
    listeners = elbv2.describe_listeners(LoadBalancerArn=alb_arn)["Listeners"]
    if listeners:
        print("✔ Listener already exists")
        return listeners[0]["ListenerArn"]
    listener = elbv2.create_listener(
        LoadBalancerArn=alb_arn,
        Protocol="HTTP",
        Port=80,
        DefaultActions=[{"Type":"forward","TargetGroupArn":tg_arn}]
    )["Listeners"][0]
    print("✔ Listener created and target group attached")
    return listener["ListenerArn"]

def wait_for_targets_healthy(tg_arn, timeout=300):
    print("⏳ Waiting for targets to become healthy...")
    start = time.time()
    while True:
        resp = elbv2.describe_target_health(TargetGroupArn=tg_arn)
        states = [t["TargetHealth"]["State"] for t in resp["TargetHealthDescriptions"]]
        print("  Target states:", states)
        if all(s == "healthy" for s in states):
            print("✔ All targets are healthy")
            return
        if time.time() - start > timeout:
            raise TimeoutError("Targets did not become healthy in time")
        time.sleep(10)

# ----------------------------------------------------
# Execution
# ----------------------------------------------------
get_or_create_keypair(overwrite=True)
vpc = get_or_create_vpc()

# Subnets
public_subnet_1 = get_or_create_subnet(vpc, "10.0.1.0/24", "public-subnet-1", public=True, az="us-east-1a")
public_subnet_2 = get_or_create_subnet(vpc, "10.0.2.0/24", "public-subnet-2", public=True, az="us-east-1b")
private_subnet_1 = get_or_create_subnet(vpc, "10.0.3.0/24", "private-subnet-1", az="us-east-1a")
private_subnet_2 = get_or_create_subnet(vpc, "10.0.4.0/24", "private-subnet-2", az="us-east-1b")

igw = get_or_create_igw(vpc)
nat_id = get_or_create_nat(public_subnet_1)

# Route Tables
public_rt = get_or_create_route_table(vpc, "public-rt")
ensure_route(public_rt, "0.0.0.0/0", igw_id=igw)
associate_rt(public_rt, public_subnet_1)
associate_rt(public_rt, public_subnet_2)

private_rt = get_or_create_route_table(vpc, "private-rt")
ensure_route(private_rt, "0.0.0.0/0", nat_id=nat_id)
associate_rt(private_rt, private_subnet_1)
associate_rt(private_rt, private_subnet_2)

# Security Groups
public_sg = get_or_create_sg(
    "public-sg", "Public SG", vpc,
    [{"IpProtocol":"tcp","FromPort":22,"ToPort":22,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]},
     {"IpProtocol":"tcp","FromPort":80,"ToPort":80,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]
)
private_sg = get_or_create_sg(
    "private-sg", "Private SG", vpc,
    [{"IpProtocol":"tcp","FromPort":22,"ToPort":22,"IpRanges":[{"CidrIp":"10.0.0.0/16"}]},
     {"IpProtocol":"tcp","FromPort":80,"ToPort":80,"IpRanges":[{"CidrIp":"10.0.0.0/16"}]},
     {"IpProtocol":"tcp","FromPort":3306,"ToPort":3306,"IpRanges":[{"CidrIp":"10.0.0.0/16"}]},
     {"IpProtocol":"icmp","FromPort":-1,"ToPort":-1,"IpRanges":[{"CidrIp":"10.0.0.0/16"}]}]
)

print("\n🎉 Networking setup completed successfully")

# Launch instances with colorful canvas pages
user_data_1 = """#!/bin/bash
yum update -y
yum install -y httpd
systemctl start httpd
systemctl enable httpd
echo '<html><body><canvas id="canvas1" width="400" height="200"></canvas><script>
c=document.getElementById("canvas1").getContext("2d");
c.fillStyle="red";c.fillRect(0,0,400,200);
c.fillStyle="white";c.font="30px Arial";c.fillText("Private Server 1",50,100);
</script></body></html>' > /var/www/html/index.html
"""

user_data_2 = """#!/bin/bash
yum update -y
yum install -y httpd
systemctl start httpd
systemctl enable httpd
echo '<html><body><canvas id="canvas2" width="400" height="200"></canvas><script>
c=document.getElementById("canvas2").getContext("2d");
c.fillStyle="blue";c.fillRect(0,0,400,200);
c.fillStyle="white";c.font="30px Arial";c.fillText("Private Server 2",50,100);
</script></body></html>' > /var/www/html/index.html
"""

private_instance_1 = launch_instance("private-ec2-1", private_subnet_1, private_sg, user_data_1)
private_instance_2 = launch_instance("private-ec2-2", private_subnet_2, private_sg, user_data_2)
public_instance = launch_instance("public-ec2-1", public_subnet_1, public_sg)

# ALB and Target Group
alb_arn = get_or_create_alb("salman-alb", [public_subnet_1, public_subnet_2], public_sg)
tg_arn = get_or_create_target_group("salman-TG", vpc)

# Register instances and create listener
elbv2.register_targets(TargetGroupArn=tg_arn, Targets=[
    {"Id": private_instance_1, "Port": 80},
    {"Id": private_instance_2, "Port": 80}
])
listener_arn = create_listener(alb_arn, tg_arn)
wait_for_targets_healthy(tg_arn)

print("\n🎯 Full infrastructure deployed successfully!")

