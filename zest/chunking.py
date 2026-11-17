"""Offline structure-aware chunks with a reproducible reference tokenizer.

cl100k_base is a proxy, not the Alibaba model tokenizer; provider limits still apply.
Column offsets are zero-based Unicode code points, with an exclusive end.
"""
import ast
from functools import lru_cache
from pathlib import Path
import re

CHUNKING_VERSION = 'structure-cl100k-v1'
MAX_TOKENS = 500
MAX_CHARS = 6000
MAX_LINES = 40
OVERLAP_LINES = 3


@lru_cache(maxsize=1)
def tokenizer():
    import tiktoken
    from tiktoken.load import load_tiktoken_bpe
    path = Path(__file__).parent / 'data' / 'cl100k_base.tiktoken'
    ranks = load_tiktoken_bpe(str(path), expected_hash='223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7')
    return tiktoken.Encoding(name='zest_cl100k_base', pat_str=r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s+""", mergeable_ranks=ranks, special_tokens={})


def token_count(text):
    return len(tokenizer().encode_ordinary(text))


def _boundaries(path, text, count):
    boundaries = {0, count}
    if path.endswith(('.py', '.pyi')):
        try:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start = min([node.lineno, *(item.lineno for item in node.decorator_list)]) - 1
                    boundaries.update((start, node.end_lineno))
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            pass
    elif path.lower().endswith(('.md', '.rst')):
        for i, line in enumerate(text.split('\n')):
            if re.match(r'^ {0,3}#{1,6}\s+\S', line):
                boundaries.add(i)
    return sorted(i for i in boundaries if 0 <= i <= count)


def _long_line(line, number):
    start = 0
    while start < len(line):
        low, high = start + 1, min(len(line), start + MAX_CHARS)
        best = start
        while low <= high:
            mid = (low + high) // 2
            if token_count(line[start:mid]) <= MAX_TOKENS:
                best, low = mid, mid + 1
            else:
                high = mid - 1
        if best == start:
            raise ValueError('single character exceeds token budget')
        yield {'start_line': number, 'end_line': number, 'start_col': start, 'end_col': best}, line[start:best]
        start = best


def chunks(path, text):
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    boundaries = _boundaries(path, text, len(lines))
    for left, right in zip(boundaries, boundaries[1:]):
        start = left
        while start < right:
            line = lines[start]
            if len(line) > MAX_CHARS or token_count(line) > MAX_TOKENS:
                yield from _long_line(line, start + 1)
                start += 1
                continue
            end = start + 1
            while end < min(right, start + MAX_LINES):
                candidate = '\n'.join(lines[start:end + 1])
                if len(candidate) > MAX_CHARS or token_count(candidate) > MAX_TOKENS:
                    break
                end += 1
            yield {'start_line': start + 1, 'end_line': end}, '\n'.join(lines[start:end])
            if end == right:
                break
            # Only overlap complete lines; always make progress.
            next_start = max(start + 1, end - OVERLAP_LINES)
            # Never place an oversized next line into an existing chunk.
            if next_start < end and end < right and (len(lines[end]) > MAX_CHARS or token_count(lines[end]) > MAX_TOKENS):
                next_start = end
            start = next_start


def slice_record(record, evidence):
    """Project verified Git lines onto an optional single-line character range."""
    lines = record['lines']
    if 'start_col' not in evidence and 'end_col' not in evidence:
        return lines
    if (len(lines) != 1 or type(evidence.get('start_col')) is not int or type(evidence.get('end_col')) is not int
            or not 0 <= evidence['start_col'] < evidence['end_col'] <= len(lines[0]['text'])):
        raise ValueError('invalid source column range')
    return [{**lines[0], 'text': lines[0]['text'][evidence['start_col']:evidence['end_col']]}]
