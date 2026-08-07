from openapi_core import OpenAPI
from pipeline.spec_loader import materialize

app = OpenAPI.from_file_path('avidea-mind-openapi.yaml')
spec_root = app.spec
paths = spec_root.get('paths', {})

print('Paths keys:', list(paths.keys()))

for path in paths.keys():
    print(f'\nPath: {path}')
    path_item = paths[path]
    print(f'  Raw type: {type(path_item)}')
    mat = materialize(path_item)
    print(f'  Materialized type: {type(mat)}')
    if isinstance(mat, dict):
        print(f'  Keys: {list(mat.keys())}')
