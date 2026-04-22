from shkeeper import create_app

def main():
    app = create_app()

    with app.app_context():
        api = app.extensions["smorest"]

        spec = api.spec.to_dict()

        import os, json
        os.makedirs("static/openapi", exist_ok=True)

        with open("static/openapi/openapi.json", "w") as f:
            json.dump(spec, f, indent=2)

        print("OpenAPI exported")

if __name__ == "__main__":
    main()