import json
import os
from shkeeper import create_app

# def normalize_openapi(spec: dict) -> dict:
#     # force correct version
#     spec["openapi"] = "3.0.3"

#     spec.setdefault("info", {})
#     spec.setdefault("paths", {})
#     spec.setdefault("components", {})

#     # fix duplicate schemas (optional but useful)
#     if "schemas" in spec["components"]:
#         seen = {}
#         fixed = {}

#         for name, schema in spec["components"]["schemas"].items():
#             if name in seen:
#                 new_name = f"{name}_{seen[name]}"
#                 seen[name] += 1
#             else:
#                 new_name = name
#                 seen[name] = 1

#             fixed[new_name] = schema

#         spec["components"]["schemas"] = fixed

#     return spec


def main():
    app = create_app()

    with app.app_context():
        api = app.extensions["smorest"]
        spec = api.spec.to_dict()

        # spec = normalize_openapi(spec)

        os.makedirs("static/openapi", exist_ok=True)

        path = "static/openapi/openapi.json"

        with open(path, "w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2, ensure_ascii=False)

        # # 🔥 DEBUG OUTPUT (proper, not broken)
        # print("First 20 lines of OpenAPI JSON:")
        # with open(path, "r", encoding="utf-8") as f:
        #     for i, line in enumerate(f):
        #         if i >= 20:
        #             break
        #         print(line.rstrip())

        # print("✅ OpenAPI exported & normalized")


if __name__ == "__main__":
    main()