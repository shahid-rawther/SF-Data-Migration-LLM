from common.logger import logger
def ensure_key(object: dict, key:str):
    if key not in object:
        logger.error(f"{key} not found in {object}")
        raise
        
    