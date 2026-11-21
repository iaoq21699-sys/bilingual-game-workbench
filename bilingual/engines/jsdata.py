"""Read static literals in an explicitly selected localization data module.

This is deliberately not a JavaScript evaluator. Calls, references, functions,
dynamic templates and other expressions are opaque. Never use it for application
entry points, preload scripts or arbitrary executable modules.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


class _Opaque:
    pass


OPAQUE = _Opaque()
MAX_DEPTH = 64
MAX_TOKENS = 500_000

_LEX = re.compile(
    r"(?P<space>\s+)|(?P<comment>//[^\r\n]*|/\*[\s\S]*?\*/)"
    r"|(?P<string>\"(?:\\[\s\S]|[^\"\\])*\"|'(?:\\[\s\S]|[^'\\])*')"
    r"|(?P<template>`(?:\\[\s\S]|[^`\\])*`)"
    r"|(?P<ident>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"|(?P<number>0[xX][0-9A-Fa-f]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)"
    r"|(?P<symbol>=>|\.\.\.|[^\s])"
)


@dataclass(frozen=True)
class Token:
    kind: str
    raw: str


def _tokens(text):
    tokens = []
    for match in _LEX.finditer(text.lstrip("\ufeff")):
        kind = match.lastgroup
        if kind not in ("space", "comment"):
            tokens.append(Token(kind, match.group()))
            if len(tokens) > MAX_TOKENS:
                raise ValueError("语言数据模块超过静态解析上限。")
    return tokens


def _string(token):
    raw = token.raw[1:-1]
    result = []
    i = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v",
               "0": "\0", "\\": "\\", "'": "'", '"': '"', "`": "`"}
    while i < len(raw):
        c = raw[i]
        if token.kind == "template" and c == "$" and raw[i:i+2] == "${":
            return OPAQUE  # Never evaluate expressions inside templates.
        if c != "\\":
            result.append(c)
            i += 1
            continue
        i += 1
        if i == len(raw):
            return OPAQUE
        c = raw[i]
        i += 1
        if c in "\r\n":
            if c == "\r" and i < len(raw) and raw[i] == "\n":
                i += 1
            continue
        if c in ("x", "u"):
            if c == "u" and raw[i:i+1] == "{":
                end = raw.find("}", i+1)
                if end < 0 or not 1 <= end-i-1 <= 6:
                    return OPAQUE
                digits, i = raw[i+1:end], end+1
            else:
                width = 2 if c == "x" else 4
                digits, i = raw[i:i+width], i+width
                if len(digits) != width:
                    return OPAQUE
            if not re.fullmatch(r"[0-9a-fA-F]+", digits):
                return OPAQUE
            try:
                result.append(chr(int(digits, 16)))
            except ValueError:
                return OPAQUE
        elif c.isdigit() and (c != "0" or raw[i:i+1].isdigit()):
            return OPAQUE  # Legacy numeric escapes are not a supported data format.
        else:
            result.append(escapes.get(c, c))
    try:
        # JS permits UTF-16 surrogate escapes; combine a valid pair, reject lone ones.
        return "".join(result).encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeError:
        return OPAQUE


class _Parser:
    def __init__(self, text):
        self.tokens = _tokens(text)
        self.i = 0
        self.opaque_values = 0

    def raw(self):
        return self.tokens[self.i].raw if self.i < len(self.tokens) else None

    def take(self, value):
        if self.raw() == value:
            self.i += 1
            return True
        return False

    def need(self, value):
        if not self.take(value):
            raise ValueError("语言数据模块不是受支持的静态对象结构。")

    def skip(self, stops):
        """Skip an unsupported expression structurally, not semantically."""
        brackets = {"(": ")", "[": "]", "{": "}"}
        stack = []
        while self.i < len(self.tokens):
            token = self.tokens[self.i]
            symbol = token.raw if token.kind == "symbol" else None
            if not stack and symbol in stops:
                return
            if symbol in brackets:
                stack.append(brackets[symbol])
                if len(stack) > MAX_DEPTH:
                    raise ValueError("语言数据表达式层级过深。")
            elif symbol in brackets.values():
                if not stack or stack.pop() != symbol:
                    raise ValueError("语言数据模块包含不受支持的表达式结构。")
            self.i += 1
        if stack:
            raise ValueError("语言数据模块表达式不完整。")

    def value(self, stops, depth):
        if depth > MAX_DEPTH:
            raise ValueError("语言数据结构层级过深。")
        start = self.i
        if self.i >= len(self.tokens) or self.raw() in stops:
            raise ValueError("语言数据缺少值。")
        token = self.tokens[self.i]
        result = OPAQUE
        if token.raw == "{":
            result = self.object(depth+1)
        elif token.raw == "[":
            result = self.array(depth+1)
        elif token.kind in ("string", "template"):
            self.i += 1
            result = _string(token)
        elif token.kind == "number":
            self.i += 1
            if len(token.raw) <= 30:
                try:
                    result = int(token.raw, 16) if token.raw.lower().startswith("0x") else (
                        int(token.raw) if token.raw.isdecimal() else float(token.raw))
                except ValueError:
                    pass
        elif token.raw in ("true", "false", "null"):
            self.i += 1
            result = {"true": True, "false": False, "null": None}[token.raw]
        if result is not OPAQUE and (self.raw() is None or self.raw() in stops):
            return result
        self.i = start
        self.skip(stops)
        if self.i == start:
            raise ValueError("语言数据值无法读取。")
        self.opaque_values += 1
        return OPAQUE

    def object(self, depth):
        self.need("{")
        result = {}
        unknown_keys = False
        while not self.take("}"):
            if self.i >= len(self.tokens):
                raise ValueError("语言数据对象未闭合。")
            token = self.tokens[self.i]
            key = (_string(token) if token.kind == "string" else token.raw
                   if token.kind in ("ident", "number") else OPAQUE)
            # Unknown/computed/spread members are not part of the static data map.
            if key is OPAQUE or self.i+1 == len(self.tokens) or self.tokens[self.i+1].raw != ":":
                self.skip({",", "}"})
                self.opaque_values += 1
                # A computed member/spread could overwrite a literal key. Do not
                # treat the rest of such an object as a verified static map.
                unknown_keys = True
            else:
                self.i += 2
                if key in result:
                    raise ValueError("语言数据对象中有重复属性，无法可靠配对。")
                result[key] = self.value({",", "}"}, depth)
            if self.take("}"):
                break
            self.need(",")
        return OPAQUE if unknown_keys else result

    def array(self, depth):
        self.need("[")
        result = []
        while not self.take("]"):
            if self.i >= len(self.tokens):
                raise ValueError("语言数据数组未闭合。")
            if self.take(","):
                result.append(OPAQUE)
                self.opaque_values += 1
                continue
            result.append(self.value({",", "]"}, depth))
            if self.take("]"):
                break
            self.need(",")
        return result

    def roots(self):
        result = []
        while self.i < len(self.tokens):
            if self.take("const"):
                while True:
                    if self.i >= len(self.tokens) or self.tokens[self.i].kind != "ident":
                        raise ValueError("语言数据存在不受支持的声明。")
                    self.i += 1
                    self.need("=")
                    value = self.value({",", ";"}, 0)
                    if isinstance(value, (dict, list)):
                        result.append(value)
                    if not self.take(","):
                        self.take(";")
                        break
            else:
                # Import/export/other statements: skip the entire statement.
                # Do not resolve symbols, inspect executable code, or run it.
                self.skip({";"})
                self.take(";")
        return result, self.opaque_values


def parse_data_module(data):
    """Return top-level static containers and a count of omitted expressions."""
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("语言数据模块过大。")
    try:
        return _Parser(data.decode("utf-8")).roots()
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("语言数据模块编码或结构不受支持。") from exc


def parse_literal_object(text):
    """Read the first object after a schema-specific, verified marker."""
    if len(text) > 50_000:
        raise ValueError("界面词表超过静态解析上限。")
    result = _Parser(text).object(1)
    if not isinstance(result, dict):
        raise ValueError("界面词表不再是可确认的静态对象。")
    return result
