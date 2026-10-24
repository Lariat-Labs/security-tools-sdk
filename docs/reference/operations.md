# Operations reference

Generated from the SDK registry at build time. Every operation can be called three ways:

```bash
secctl <provider> <operation> ...                  # CLI
secctl call <provider>.<operation> --params '{…}'  # CLI, by name
```

```python
client.call("<provider>.<operation>", **params)  # Python
```

<!-- secsdk:operations -->
