"""Project knowledge entry point used by the actual local Qwen tool chain."""
from pathlib import Path
from .project_knowledge_v3 import VERSION,retrieve

def lookup(topic,root=None):
    return retrieve(root or Path(__file__).resolve().parents[1],topic)
