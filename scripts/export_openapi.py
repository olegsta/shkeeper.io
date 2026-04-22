import os
import json
from shkeeper import create_app


def main():
    app = create_app()

    with app.app_context():
        api = app.extensions["api"]   # ✅ ВАЖНО
        spec = api.spec.to_dict()

        output = "static/openapi/openapi.json"
        os.makedirs(os.path.dirname(output), exist_ok=True)

        with open(output, "w") as f:
            json.dump(spec, f, indent=2)

        print(f"OpenAPI exported -> {output}")


if __name__ == "__main__":
    main()