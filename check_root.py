import glob
import os
import ast

def get_class_methods(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    
    tree = ast.parse(content)
    methods = []
    
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and not item.name.startswith('_'):
                    is_classmethod = any(isinstance(d, ast.Name) and d.id == 'classmethod' for d in item.decorator_list)
                    is_staticmethod = any(isinstance(d, ast.Name) and d.id == 'staticmethod' for d in item.decorator_list)
                    methods.append({
                        'class': node.name,
                        'name': item.name,
                        'is_classmethod': is_classmethod,
                        'is_staticmethod': is_staticmethod,
                        'args': [a.arg for a in item.args.args]
                    })
        elif isinstance(node, ast.FunctionDef) and not node.name.startswith('_'):
            methods.append({
                'class': None,
                'name': node.name,
                'is_classmethod': False,
                'is_staticmethod': False,
                'args': [a.arg for a in node.args.args]
            })
    return methods

def main():
    for path in glob.glob('src/pytest_mes_core/*.py'):
        if os.path.basename(path) in ['__init__.py', 'plugin.py']:
            continue
        methods = get_class_methods(path)
        with open(path, 'r') as f:
            content = f.read()
            
        print(f"\n--- {os.path.basename(path)} ---")
        for m in methods:
            # check if it already has an async variant or is inherently async
            if m['name'].startswith('async_'):
                continue
            has_async = f"async def async_{m['name']}" in content
            # check if it's an async function definition
            is_async = f"async def {m['name']}" in content
            if not has_async and not is_async:
                print(f"Missing async: {m['class'] + '.' if m['class'] else ''}{m['name']}")

if __name__ == '__main__':
    main()
