import json
from pathlib import Path
from requests import Response


def save_stream_to_file(resp: Response, path: Path, chunk_size: int = 1024 * 1024):
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk:  # filter keep-alive chunks
                f.write(chunk)
    
def save_data_to_file(content: bytes, path: Path):
    with open(path, "wb") as f:
        f.write(content)

def save_text_to_file(content: str, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def read_text_from_file(path: Path):
    content = ""
    with open(path, "r") as f:
        content = f.read()
    return content

def save_json_to_file(data, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps(data))


def read_json_from_file(path: Path):
    contents = ""
    with open(path, "r") as f:
        contents = f.read()
    return json.loads(contents) if contents else []