import os
import json

from shkeeper import create_app


def main():
    app = create_app()

    # гарантируем контекст Flask
    with app.app_context():
        spec = app.openapi()

        output = "static/openapi/openapi.json"
        os.makedirs(os.path.dirname(output), exist_ok=True)

        with open(output, "w") as f:
            json.dump(spec, f, indent=2)

        print(f"OpenAPI exported to {output}")


if __name__ == "__main__":
    main()