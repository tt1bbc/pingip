from flask import Blueprint, render_template, request, jsonify
import netaddr
import asyncio
import aioping
import socket
from .config import Config  # 确保导入 Config

main = Blueprint('main', __name__)

async def pingip(host):
    """异步 ping IP 地址"""
    try:
        delay = await aioping.ping(host, timeout=2)
        return host, round(delay, 5)
    except TimeoutError:
        return host, "TimeOut"
    except OSError as e:
        return host, str(e)

@main.route('/')
def index():
    """主页"""
    return render_template('index.html', comrange=sorted(Config.COMRANGE, reverse=True))

@main.route('/ping', methods=['POST'])
async def ping():
    """处理 ping 请求"""
    ip_range = request.form.get('ip_range')
    
    if '-' in ip_range:
        ip_range = ip_range.split('-')[0].strip()
    
    success_results = {}
    fail_results = {}
    
    try:
        net = netaddr.IPNetwork(ip_range)
        tasks = [pingip(str(host)) for host in net.iter_hosts()]
        pingresults = await asyncio.gather(*tasks)

        for reshost, result in pingresults:
            if isinstance(result, float):  # Ping 成功
                success_results[reshost] = result
            else:  # Ping 失败
                fail_results[reshost] = result
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"success": success_results, "fail": fail_results})



@main.route('/search', methods=['GET'])
def search():
    """模糊搜索功能"""
    query = request.args.get('query', '').lower()
    filtered_values = [item for item in Config.COMRANGE if query in item.lower()]
    return jsonify(filtered_values)
