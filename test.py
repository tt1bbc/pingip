import requests
from xml.etree import ElementTree as ET
JENKINS_URL = 'https://mrojenkins.loongair.cn'
JENKINS_USER = 'linhaoli'
JENKINS_API_TOKEN = '11c91f068a495c809f4afeed3cbcef947f'
job_config_url = f"{JENKINS_URL}/job/test-ms-provider-serve-nsms/config.xml"
response = requests.get(job_config_url, auth=(JENKINS_USER, JENKINS_API_TOKEN))
if response.status_code == 200:
    root = ET.fromstring(response.text)
    for param_def in root.findall(".//parameterDefinitions/*"):
        name = param_def.find("name").text if param_def.find("name") is not None else "Unknown"
        default_value = param_def.find(".//defaultValue").text if param_def.find(".//defaultValue") is not None else "No Default Value"
        print(f"Parameter: {name}, Default Value: {default_value}")