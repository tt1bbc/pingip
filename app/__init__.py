from flask import Flask

def create_app():
    app = Flask(__name__, template_folder='templates')  # 指定模板目录（可选）
    
    # 加载配置
    app.config.from_object('app.config.Config')
    
    # 注册路由
    from .routes import main
    app.register_blueprint(main)
    
    return app
