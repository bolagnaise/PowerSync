# config.py
import os
import subprocess
from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

def get_version():
    """Get the current git commit hash as version identifier."""
    # Try VERSION file first (Docker deployment)
    version_file = os.path.join(basedir, 'VERSION')
    if os.path.exists(version_file):
        try:
            with open(version_file, 'r') as f:
                version = f.read().strip()
                if version and version != 'unknown':
                    return version
        except Exception:
            pass

    # Fall back to git command (development)
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--short=7', 'HEAD'],
            cwd=basedir,
            capture_output=True,
            text=True,
            timeout=1
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return 'unknown'

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-please-change-in-production'

    # Use DATABASE_URL if set (for Docker/PostgreSQL), otherwise use SQLite
    # In Docker, this will be /app/data/app.db (persisted in volume)
    # Locally, this will be in the project root directory
    default_db_path = os.path.join(basedir, 'data', 'app.db') if os.path.exists(os.path.join(basedir, 'data')) else os.path.join(basedir, 'app.db')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///' + default_db_path
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # SQLite-specific settings to reduce locking issues
    # Increase busy timeout to 30 seconds (default is 5 seconds)
    SQLALCHEMY_ENGINE_OPTIONS = {
        'connect_args': {
            'timeout': 30,  # SQLite busy timeout in seconds
            'check_same_thread': False,  # Allow multi-threaded access
        },
        'pool_pre_ping': True,  # Verify connections before use
    }
