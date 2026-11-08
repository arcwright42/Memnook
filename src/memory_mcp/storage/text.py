import logging
import re

import jieba

jieba.setLogLevel(logging.WARNING)
_tokenizer = jieba.Tokenizer()
_pieces = re.compile(r"[\u3400-\u9fff]+|[A-Za-z0-9_]+(?:[.+#/-][A-Za-z0-9_+#]+)*")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for piece in _pieces.findall(text):
        if "\u3400" <= piece[0] <= "\u9fff":
            tokens.extend(part for part in _tokenizer.cut_for_search(piece) if part.strip())
        else:
            tokens.append(piece.casefold())
            tokens.extend(
                part.casefold()
                for part in re.sub(r"([a-z])([A-Z])", r"\1 \2", piece).replace("_", " ").split()
            )
    return tokens


def query_text(text: str) -> str:
    return " OR ".join(f'"{term}"' for term in dict.fromkeys(tokenize(text)))
