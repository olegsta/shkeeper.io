from shkeeper import create_app
import json
import os

def main():
    app = create_app()

    with app.app_context():
        from shkeeper import api

        spec = api.spec.to_dict()

        path = "static/openapi/openapi.json"
        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, "w") as f:
            json.dump(spec, f, indent=2)

        print("OpenAPI exported ->", path)

if __name__ == "__main__":
    main()