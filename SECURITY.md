# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Instead:

- open a [private security advisory](https://github.com/popnow-ai/popnow-blender/security/advisories/new) on GitHub, or
- email **support@popnow.ai** (see also [popnow.ai/contact](https://popnow.ai/contact)).

Include the add-on version, Blender version, steps to reproduce and the impact. Never send a real API key; if a key may have leaked, revoke it right away in the [API console](https://popnow.ai/api/console/keys).

## Supported versions

Security fixes are released for the latest version of the add-on.

## How the add-on handles your key

- The API key is stored only in Blender's user preferences, never in `.blend` files, and the UI only shows a masked form.
- Downloads of generated files use short-lived signed URLs and never send the key.
- The add-on sends no requests while Blender's **Allow Online Access** is off.
