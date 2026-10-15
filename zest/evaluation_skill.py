"""Versioned built-in evaluator skill; never loads project-provided instructions."""
from pathlib import Path

SKILL_PATH = Path(__file__).parent / 'skills' / 'cognition-evaluation' / 'SKILL.md'


def skill_text():
    return SKILL_PATH.read_text(encoding='utf-8')
