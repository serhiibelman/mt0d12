

# 1. Create an IoT Thing

## Step 1 — create the Terraform files

```
terraform/
├── main.tf
├── variables.tf
├── outputs.tf
└── iot.tf
```
Do not commit secrets/

## Step 2 — make sure your AWS provider exists

```
terraform init
terraform plan
terraform apply
```

## Step 3 — create a local certs directory on the Pi

SSH into the Pi:

```commandline
mkdir -p ~/mt0d12/certs
chmod 700 ~/mt0d12/certs
```

We need these three files:

```
~/mt0d12/certs/
├── device.pem.crt
├── private.pem.key
└── Amazon-root-CA-1.pem
```

AWS uses the device certificate + private key for client authentication, and the Root CA to verify AWS IoT's server certificate.

## Step 4 — get the Terraform-generated certificate/key

Then copy secrets to the Pi:

```
scp secrets/device.pem.crt pi@<PI_IP>:~/mt0d12/certs/
scp secrets/private.pem.key pi@<PI_IP>:~/mt0d12/certs/
```
And on the Pi:
```commandline
chmod 644 ~/mt0d12/certs/device.pem.crt
chmod 600 ~/mt0d12/certs/private.pem.key
```
## Step 5 — Root CA
Download the AWS Root CA separately:
```
wget https://www.amazontrust.com/repository/AmazonRootCA1.pem \
  -O ~/mt0d12/certs/Amazon-root-CA-1.pem
```
## install the MQTT client
```
python -c "import paho.mqtt.client; print('paho-mqtt OK')"
```
## Check AWS
In AWS IoT Core → MQTT test client:

1. Click Subscribe
2. Topic: `rover/rover-01/telemetry`
3. Subscribe