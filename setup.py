"""
AI Viral Content Factory - Setup Configuration

This setup.py file handles:
- Package metadata and versioning
- Dependency management for core and optional features
- Package discovery and module organization
- CLI entry points for factory operations
- Python version enforcement (>=3.11 for async/typing features)

Author: Alan
Version: 1.0.0
License: MIT
"""

from setuptools import setup, find_packages
from pathlib import Path

# Read the README for long description
this_directory = Path(__file__).parent
long_description = (this_directory / "README.md").read_text(encoding="utf-8")

# Read version from a dedicated file to maintain single source of truth
version_file = this_directory / "src" / "VERSION"
if version_file.exists():
    VERSION = version_file.read_text(encoding="utf-8").strip()
else:
    VERSION = "1.0.0"

# Core dependencies required for basic operation
CORE_REQUIREMENTS = [
    # Data manipulation and analysis
    "pandas>=2.0.0",
    "numpy>=1.24.0",
    
    # Machine Learning / Deep Learning
    "scikit-learn>=1.3.0",
    "torch>=2.1.0",
    "torchvision>=0.16.0",
    
    # Configuration and serialization
    "pyyaml>=6.0",
    "python-dotenv>=1.0.0",
    
    # Async operations and HTTP
    "aiohttp>=3.9.0",
    "aiofiles>=23.0.0",
    "httpx>=0.25.0",
    
    # Progress tracking and CLI
    "tqdm>=4.66.0",
    "click>=8.1.0",
    "rich>=13.0.0",  # Beautiful CLI output
    
    # Database and caching
    "sqlalchemy>=2.0.0",
    "redis>=5.0.0",
    "psycopg2-binary>=2.9.0",
    
    # API clients
    "requests>=2.31.0",
    "google-api-python-client>=2.100.0",
    "google-auth>=2.23.0",
    
    # Content generation
    "openai>=1.0.0",
    "anthropic>=0.7.0",
    
    # Video/Audio processing
    "moviepy>=1.0.3",
    "pillow>=10.0.0",
    "opencv-python>=4.8.0",
    
    # Scheduling and async tasks
    "celery>=5.3.0",
    "apscheduler>=3.10.0",
    
    # Monitoring and logging
    "loguru>=0.7.0",
    "sentry-sdk>=1.38.0",
    
    # Utilities
    "python-dateutil>=2.8.0",
    "pytz>=2023.3",
    "validators>=0.22.0",
]

# Optional dependencies for development
DEV_REQUIREMENTS = [
    # Testing
    "pytest>=7.4.0",
    "pytest-cov>=4.1.0",
    "pytest-asyncio>=0.21.0",
    "pytest-mock>=3.12.0",
    "pytest-xdist>=3.5.0",  # Parallel test execution
    
    # Code quality
    "black>=23.0.0",
    "flake8>=6.1.0",
    "pylint>=3.0.0",
    "mypy>=1.7.0",
    "isort>=5.12.0",
    
    # Security
    "bandit>=1.7.5",
    "safety>=2.3.0",
    
    # Documentation
    "sphinx>=7.2.0",
    "sphinx-rtd-theme>=2.0.0",
    
    # Development tools
    "ipython>=8.18.0",
    "jupyter>=1.0.0",
    "jupyterlab>=4.0.0",
    "pre-commit>=3.5.0",
]

# Optional dependencies for ML experimentation
ML_REQUIREMENTS = [
    "transformers>=4.35.0",
    "datasets>=2.15.0",
    "tensorboard>=2.15.0",
    "wandb>=0.16.0",
    "optuna>=3.5.0",  # Hyperparameter optimization
    "ray[tune]>=2.9.0",  # Distributed hyperparameter tuning
]

# Optional dependencies for production deployment
PROD_REQUIREMENTS = [
    "gunicorn>=21.2.0",
    "uvicorn>=0.24.0",
    "fastapi>=0.104.0",
    "prometheus-client>=0.19.0",
    "datadog>=0.48.0",
]

# Optional dependencies for enhanced video processing
VIDEO_REQUIREMENTS = [
    "ffmpeg-python>=0.2.0",
    "imageio>=2.33.0",
    "imageio-ffmpeg>=0.4.9",
    "youtube-dl>=2021.12.17",
    "yt-dlp>=2023.11.16",  # Modern youtube-dl fork
]

# Optional dependencies for NLP and text generation
NLP_REQUIREMENTS = [
    "spacy>=3.7.0",
    "nltk>=3.8.1",
    "textblob>=0.17.1",
    "langchain>=0.0.340",
]

setup(
    # Package metadata
    name="ai-viral-content-factory",
    version=VERSION,
    description="AI-powered multi-niche viral content factory with RL-driven optimization",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Alan",
    author_email="alan@viral-factory.ai",
    url="https://github.com/yourorg/ai-viral-content-factory",
    license="MIT",
    
    # Python version requirement
    python_requires=">=3.11",
    
    # Package discovery
    packages=find_packages(
        where=".",
        exclude=[
            "tests*",
            "notebooks*",
            "docs*",
            "scripts*",
            "*.egg-info",
            "__pycache__",
        ]
    ),
    
    # Include non-Python files specified in MANIFEST.in
    include_package_data=True,
    
    # Core dependencies
    install_requires=CORE_REQUIREMENTS,
    
    # Optional dependency groups
    extras_require={
        "dev": DEV_REQUIREMENTS,
        "ml": ML_REQUIREMENTS,
        "prod": PROD_REQUIREMENTS,
        "video": VIDEO_REQUIREMENTS,
        "nlp": NLP_REQUIREMENTS,
        "all": (
            DEV_REQUIREMENTS +
            ML_REQUIREMENTS +
            PROD_REQUIREMENTS +
            VIDEO_REQUIREMENTS +
            NLP_REQUIREMENTS
        ),
    },
    
    # CLI entry points
    entry_points={
        "console_scripts": [
            # Main factory management
            "run-factory=src.factories.factory_manager:main",
            "factory-cli=src.cli.main:cli",
            
            # Individual components
            "scrape-trends=src.data.trend_scraper:main",
            "train-model=src.ml.train:main",
            "evaluate-content=src.evaluation.evaluator:main",
            "post-content=src.posting.publisher:main",
            
            # Utilities
            "factory-dashboard=src.dashboard.app:main",
            "factory-monitor=src.monitoring.monitor:main",
            "factory-db-migrate=src.database.migrate:main",
        ],
    },
    
    # Package classifiers for PyPI
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Multimedia :: Video",
        "Operating System :: OS Independent",
        "Natural Language :: English",
    ],
    
    # Keywords for discoverability
    keywords=[
        "ai",
        "viral-content",
        "machine-learning",
        "reinforcement-learning",
        "content-generation",
        "video-automation",
        "youtube",
        "tiktok",
        "social-media",
    ],
    
    # Project URLs
    project_urls={
        "Bug Reports": "https://github.com/yourorg/ai-viral-content-factory/issues",
        "Documentation": "https://docs.viral-factory.ai",
        "Source": "https://github.com/yourorg/ai-viral-content-factory",
        "Changelog": "https://github.com/yourorg/ai-viral-content-factory/blob/main/CHANGELOG.md",
    },
    
    # Zip safety (set to False if package includes C extensions)
    zip_safe=False,
)