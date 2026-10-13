"""Vordium MCP server: read the Vordium network from any MCP client, and trade as a policy-limited agent.

Runs on your machine over stdio and reads the Vordium testnet unless told otherwise. Every value is read from the
network when a tool is called; nothing about the chain is stored here.
"""
__version__ = "0.3.0"
