import boto3
import time
import random
import string
import os

# Configuration parameters
REGION = "us-east-1"
KEY_NAME = "salman-key"
VPC_NAME = "salman-vpc"
AMI_ID = "ami-0532be01f26a3de55"  # Update with your preferred AMI
INSTANCE_TYPE = "t2.micro"

# Initialize boto3 clients with the specified region
ec2 = boto3.client('ec2', region_name=REGION)
elbv2 = boto3.client('elbv2', region_name=REGION)


# Generate a random name suffix for uniqueness
def random_string(length=6):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


# Wait until resource is available (health check function)
def wait_for_resource(resource_id, resource_type, check_func):
    while not check_func(resource_id):
        print(f"Waiting for {resource_type} {resource_id} to become available...")
        time.sleep(30)  # Wait for 30 seconds before checking again


# Health check for VPC
def check_vpc(vpc_id):
    response = ec2.describe_vpcs(VpcIds=[vpc_id])
    return response['Vpcs'][0]['State'] == 'available'


# Health check for Internet Gateway
def check_internet_gateway(igw_id):
    response = ec2.describe_internet_gateways(InternetGatewayIds=[igw_id])
    return any(igw['Attachments'][0]['State'] == 'available' for igw in response['InternetGateways'])


# Health check for Subnet
def check_subnet(subnet_id):
    response = ec2.describe_subnets(SubnetIds=[subnet_id])
    return response['Subnets'][0]['State'] == 'available'


# Health check for NAT Gateway
def check_nat_gateway(nat_gateway_id):
    response = ec2.describe_nat_gateways(NatGatewayIds=[nat_gateway_id])
    return response['NatGateways'][0]['State'] == 'available'


# Health check for EC2 instance
def check_instance(instance_id):
    response = ec2.describe_instances(InstanceIds=[instance_id])
    return response['Reservations'][0]['Instances'][0]['State']['Name'] == 'running'


# Step 1: Create VPC
def create_vpc():
    response = ec2.create_vpc(CidrBlock='10.0.0.0/16')
    vpc_id = response['Vpc']['VpcId']
    wait_for_resource(vpc_id, 'VPC', check_vpc)
    print(f"VPC created with ID: {vpc_id}")
    return vpc_id


# Step 2: Create and attach Internet Gateway
def create_internet_gateway(vpc_id):
    response = ec2.create_internet_gateway()
    igw_id = response['InternetGateway']['InternetGatewayId']
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    wait_for_resource(igw_id, 'Internet Gateway', check_internet_gateway)
    print(f"Internet Gateway created and attached with ID: {igw_id}")
    return igw_id


# Step 3: Create public subnets
def create_public_subnet(vpc_id, cidr_block, az):
    response = ec2.create_subnet(CidrBlock=cidr_block, VpcId=vpc_id, AvailabilityZone=az)
    subnet_id = response['Subnet']['SubnetId']
    wait_for_resource(subnet_id, 'Public Subnet', check_subnet)
    print(f"Public Subnet created with ID: {subnet_id}")
    return subnet_id


# Step 4: Create private subnets
def create_private_subnet(vpc_id, cidr_block, az):
    response = ec2.create_subnet(CidrBlock=cidr_block, VpcId=vpc_id, AvailabilityZone=az)
    subnet_id = response['Subnet']['SubnetId']
    wait_for_resource(subnet_id, 'Private Subnet', check_subnet)
    print(f"Private Subnet created with ID: {subnet_id}")
    return subnet_id


# Step 5: Create route tables
def create_route_table(vpc_id, igw_id=None, nat_gateway_id=None):
    route_table = ec2.create_route_table(VpcId=vpc_id)
    route_table_id = route_table['RouteTable']['RouteTableId']

    # Add routes to the route table
    if igw_id:
        ec2.create_route(RouteTableId=route_table_id, DestinationCidrBlock='0.0.0.0/0', GatewayId=igw_id)
    if nat_gateway_id:
        ec2.create_route(RouteTableId=route_table_id, DestinationCidrBlock='0.0.0.0/0', NatGatewayId=nat_gateway_id)

    print(f"Route Table created with ID: {route_table_id}")
    return route_table_id


# Step 6: Create a NAT Gateway
def create_nat_gateway(public_subnet_id):
    allocation_response = ec2.allocate_address(Domain='vpc')
    allocation_id = allocation_response['AllocationId']

    response = ec2.create_nat_gateway(SubnetId=public_subnet_id, AllocationId=allocation_id)
    nat_gateway_id = response['NatGateway']['NatGatewayId']
    wait_for_resource(nat_gateway_id, 'NAT Gateway', check_nat_gateway)
    print(f"NAT Gateway created with ID: {nat_gateway_id}")
    return nat_gateway_id


# Step 7: Create Security Groups
def create_security_group(vpc_id, name, inbound_ports):
    sg = ec2.create_security_group(GroupName=name, Description=f'{name} Security Group', VpcId=vpc_id)
    sg_id = sg['GroupId']

    # Add inbound rules to security group
    for port in inbound_ports:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpProtocol='tcp',
            FromPort=port,
            ToPort=port,
            CidrIp='0.0.0.0/0'
        )

    print(f"Security Group created with ID: {sg_id}")
    return sg_id


# Step 8: Create EC2 instances
def create_ec2_instances(subnet_id, sg_id, name, key_name):
    user_data = """#!/bin/bash
    yum install -y httpd
    service httpd start
    echo '<html><body><h1>Welcome to {}</h1><p>Instance ID: $(curl http://169.254.169.254/latest/meta-data/instance-id)</p></body></html>' > /var/www/html/index.html
    """
    user_data = user_data.format(name)

    response = ec2.run_instances(
        ImageId=AMI_ID,  # Use the specified AMI ID
        InstanceType=INSTANCE_TYPE,  # Use the specified instance type
        SubnetId=subnet_id,
        SecurityGroupIds=[sg_id],
        MinCount=1,
        MaxCount=1,
        KeyName=key_name,  # Use the specified key name
        UserData=user_data,
        TagSpecifications=[{
            'ResourceType': 'instance',
            'Tags': [{'Key': 'Name', 'Value': name}]
        }]
    )

    instance_id = response['Instances'][0]['InstanceId']
    wait_for_resource(instance_id, 'EC2 Instance', check_instance)
    print(f"EC2 instance {name} created with ID: {instance_id}")
    return instance_id


# Step 9: Create EC2 Key Pair (and delete if exists)
def create_key_pair(key_name):
    # Check if the key pair already exists
    try:
        ec2.describe_key_pairs(KeyNames=[key_name])
        print(f"Key pair {key_name} already exists. Deleting it first.")
        ec2.delete_key_pair(KeyName=key_name)
        time.sleep(5)  # Allow time for key deletion
    except ec2.exceptions.ClientError as e:
        if 'InvalidKeyPair.NotFound' not in str(e):
            raise e
        print(f"Key pair {key_name} does not exist, proceeding with creation.")

    # Create new key pair
    response = ec2.create_key_pair(KeyName=key_name)
    private_key = response['KeyMaterial']

    # Save private key to a file
    with open(f"{key_name}.pem", "w") as file:
        file.write(private_key)
    print(f"Key pair {key_name} created and saved to {key_name}.pem")


# Step 10: Create Load Balancer
# Step 10: Create Load Balancer
def create_load_balancer(subnet_ids):
    response = elbv2.create_load_balancer(
        Name='sal-ALB',
        Subnets=subnet_ids,  # Replace with actual subnet IDs
        SecurityGroups=[public_sg],  # Replace with actual security group ID
        Scheme='internet-facing',
        Type='application',  # Specify the load balancer type (application load balancer)
        IpAddressType='ipv4'
    )
    lb_arn = response['LoadBalancers'][0]['LoadBalancerArn']
    print(f"Load Balancer created with ARN: {lb_arn}")

    # Wait for the Load Balancer to become active
    def check_load_balancer_active(lb_arn):
        response = elbv2.describe_load_balancers(LoadBalancerArns=[lb_arn])
        state = response['LoadBalancers'][0]['State']['Code']
        return state == 'active'

    wait_for_resource(lb_arn, 'Load Balancer', check_load_balancer_active)
    print(f"Load Balancer {lb_arn} is active.")

    # Optionally set attributes (e.g., disable deletion protection)
    elbv2.modify_load_balancer_attributes(
        LoadBalancerArn=lb_arn,
        Attributes=[{'Key': 'deletion_protection.enabled', 'Value': 'false'}]
    )

    print(f"Deletion protection for Load Balancer {lb_arn} disabled.")
    return lb_arn

# Step 11: Create Target Group
def create_target_group(instances):
    # Extract the instance IDs
    instance_ids = [instance for instance in instances]

    # Create the target group
    response = elbv2.create_target_group(
        Name='sal-target-group',
        Protocol='HTTP',
        Port=80,
        VpcId=vpc_id,
        TargetType='instance'
    )

    target_group_arn = response['TargetGroups'][0]['TargetGroupArn']
    print(f"Target Group created with ARN: {target_group_arn}")

    # Register the instances with the target group
    elbv2.register_targets(TargetGroupArn=target_group_arn, Targets=[{'Id': id} for id in instance_ids])
    print(f"Instances {instance_ids} added to the target group.")

    return target_group_arn


# Main Execution
create_key_pair(KEY_NAME)

vpc_id = create_vpc()
igw_id = create_internet_gateway(vpc_id)

# Create public subnets in different Availability Zones
public_subnet1 = create_public_subnet(vpc_id, '10.0.1.0/24', 'us-east-1a')
public_subnet2 = create_public_subnet(vpc_id, '10.0.3.0/24', 'us-east-1b')

# Create private subnets
private_subnet1 = create_private_subnet(vpc_id, '10.0.2.0/24', 'us-east-1a')
private_subnet2 = create_private_subnet(vpc_id, '10.0.4.0/24', 'us-east-1b')

# Create route tables
route_table_id = create_route_table(vpc_id, igw_id)

# Create NAT Gateway in a public subnet
nat_gateway_id = create_nat_gateway(public_subnet1)

# Create Security Groups
public_sg = create_security_group(vpc_id, 'public-sg', [80, 22, 8])  # HTTP, SSH, ICMP
private_sg = create_security_group(vpc_id, 'private-sg', [80, 22, 8])  # HTTP, SSH, ICMP

# Create EC2 instances
public_instance1 = create_ec2_instances(public_subnet1, public_sg, 'public1_ec2', KEY_NAME)
public_instance2 = create_ec2_instances(public_subnet2, public_sg, 'public2_ec2', KEY_NAME)

private_instance1 = create_ec2_instances(private_subnet1, private_sg, 'private1_ec2', KEY_NAME)
private_instance2 = create_ec2_instances(private_subnet2, private_sg, 'private2_ec2', KEY_NAME)

# Create Load Balancer and Target Group
lb_arn = create_load_balancer([public_subnet1, public_subnet2])
target_group_arn = create_target_group([private_instance1, private_instance2])

# Attach the target group to the load balancer


print("Infrastructure setup completed successfully!")
