"""Container entry point; credentials remain in mounted files, not command arguments."""
import os
import sys
from pathlib import Path
from .config import load_settings
from .bootstrap import initialize

def main():
    secret=os.environ.get('SEREIN_HTTP_TOKEN_FILE')
    if secret:os.environ['SEREIN_HTTP_TOKEN']=Path(secret).read_text('utf-8').strip()
    if not os.environ.get('SEREIN_HTTP_TOKEN'):raise ValueError('API token is required')
    if '--config' in sys.argv:
        settings=load_settings(sys.argv[sys.argv.index('--config')+1])
        settings.database.parent.mkdir(parents=True,exist_ok=True)
        initialize(settings)
    from .cli import main as cli
    cli()

if __name__=='__main__':main()
