from app import create_app

app = create_app()

if __name__ == '__main__':
    app.run(host='10.102.144.153', port=5000, debug=True)
