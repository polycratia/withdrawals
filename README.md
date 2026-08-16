# withdrawals

Withdrawal pipeline: route internal transfers off-chain, send external ones
on-chain, stay idempotent when the callback arrives twice.

## Status

Pre-alpha. The package is installable but does not implement the pipeline yet.

## Installation

```bash
pip install withdrawals
```

From a checkout:

```bash
pip install -e .
```

## Usage

```python
import withdrawals

print(withdrawals.__version__)
```

## License

MIT, see [LICENSE](LICENSE).
