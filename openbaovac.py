import requests

client = hvac.Client(url="https://10.119.253.61:8200", verify="./ca.pem")
response = client.sys.is_sealed()

response2 = requests.get("https://10.119.253.61:8200/v1/sys/seal-status", verify="./ca.pem")


print(response)

print(response2.json())
