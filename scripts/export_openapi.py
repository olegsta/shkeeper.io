import json
import os
from shkeeper import create_app

def normalize_openapi(spec: dict) -> dict:
    # 🔥 FORCE valid OpenAPI version
    spec["openapi"] = "3.0.3"

    # 🔥 ensure required root fields exist
    spec.setdefault("info", {})
    spec.setdefault("paths", {})
    spec.setdefault("components", {})

    # 🔥 FIX duplicate schema names (Redocly killer)
    if "schemas" in spec["components"]:
        seen = {}
        fixed = {}

        for name, schema in spec["components"]["schemas"].items():
            if name in seen:
                new_name = f"{name}_{seen[name]}"
                seen[name] += 1
            else:
                new_name = name
                seen[name] = 1

            fixed[new_name] = schema

        spec["components"]["schemas"] = fixed

    return spec


def main():
    app = create_app()

    with app.app_context():
        api = app.extensions["smorest"]
        spec = api.spec.to_dict()

        spec = normalize_openapi(spec)

        os.makedirs("static/openapi", exist_ok=True)

        with open("static/openapi/openapi.json", "w") as f:
            json.dump(spec, f, indent=2)

        print("✅ OpenAPI exported & normalized")


if __name__ == "__main__":
    main()