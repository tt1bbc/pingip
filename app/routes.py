from flask import Blueprint, render_template, request, jsonify
import netaddr
import asyncio
import aioping
import jenkins
import socket
import requests
from xml.etree import ElementTree as ET
from .config import Config  # 确保导入 Config

main = Blueprint('main', __name__)

async def pingip(host):
    """异步 ping IP 地址"""
    try:
        delay = await aioping.ping(host, timeout=2)
        return host, round(delay, 3)
    except TimeoutError:
        return host, "TimeOut"
    except OSError as e:
        return host, str(e)

@main.route('/')
def index():
    """主页"""
    return render_template('base.html')

@main.route('/pinglist')
def pinglist():
    """Ping 网段页面"""
    return render_template('ping.html', comrange=sorted(Config.COMRANGE, reverse=True))

@main.route('/ping', methods=['POST'])
async def ping():
    """处理 ping 请求"""
    ip_range = request.form.get('ip_range')
    print(f"Received IP range: {ip_range}")

    if not ip_range:
        return jsonify({"error": "IP range is required."}), 400

    if '-' in ip_range:
        ip_range = ip_range.split('-')[0].strip()

    success_results = {}
    fail_results = {}
    
    try:
        # 检查 IP 范围是否有效
        net = netaddr.IPNetwork(ip_range)
        tasks = [pingip(str(host)) for host in net.iter_hosts()]
        pingresults = await asyncio.gather(*tasks)

        for reshost, result in pingresults:
            if isinstance(result, float):  # Ping 成功
                success_results[reshost] = result
            else:  # Ping 失败
                fail_results[reshost] = result
    except Exception as e:
        print(f"Error: {str(e)}")  # 打印异常信息
        return jsonify({"error": str(e)}), 400

    return jsonify({"success": success_results, "fail": fail_results})

# @main.route('/jenkins', methods=['GET'])
# def jenkins():
#     return render_template('jenkins.html')

@main.route('/jenkins', methods=['GET'])
def getjenkinsjobs():
    jenkins_url = Config.JENKINS_URL
    user = Config.JENKINS_USER
    api_token = Config.JENKINS_API_TOKEN
    server = jenkins.Jenkins(jenkins_url, username=user, password=api_token)
    jobs = server.get_all_jobs()
    jobsname = [job['name'] for job in jobs]
    return render_template('jenkins.html', jobs=jobsname)

@main.route('/jenkins/job-parameters', methods=['POST'])
def get_job_parameters():
    # 从前端获取 Job 名称
    job_name = request.json.get('job_name')

    if not job_name:
        return jsonify({"error": "Job name is required"}), 400

    try:
        job_config_url = f"{Config.JENKINS_URL}/job/{job_name}/config.xml"
        build_url = f"{Config.JENKINS_URL}/job/{job_name}/buildWithParameters"
        response = requests.get(job_config_url, auth=(Config.JENKINS_USER, Config.JENKINS_API_TOKEN))
        if response.status_code == 200:
            # 解析 XML
            root = ET.fromstring(response.text)
            parameters = []
            build_parameters = {}
            # 提取参数默认值
            for param_def in root.findall(".//parameterDefinitions/*"):
                name = param_def.find("name").text if param_def.find("name") is not None else "Unknown"
                default_value = param_def.find(".//defaultValue").text if param_def.find(".//defaultValue") is not None else ""
                parameters.append({'name':name, 'default':default_value})
                build_parameters[name] = default_value
        else:
            print(f"Failed to fetch job configuration: {response.status_code}")
        bresponse = requests.post(build_url, auth=(Config.JENKINS_USER, Config.JENKINS_API_TOKEN), params=default_value)
        if bresponse.status_code == 201:
            print("Build with parameters triggered successfully.")
        else:
            print(f"Failed to trigger build. Status code: {response.status_code}, Response: {response.text}")
        return jsonify(parameters)
    
    except jenkins.JenkinsException as e:
        return jsonify({"error": str(e)}), 500
