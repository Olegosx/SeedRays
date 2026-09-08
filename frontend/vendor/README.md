# Vendored libraries

Policy: every third-party library is a reviewed, fixed-version file stored in this
directory with its license next to it; no CDNs in production; updating a library is a
deliberate separate task (see ADR-0012).

| Library | Version | Source (pinned) |
|---------|---------|-----------------|
| Tabler | @tabler/core 1.5.0 | `cdn.jsdelivr.net/npm/@tabler/core@1.5.0/dist/` |
| Tabler Icons (webfont) | 3.46.0 | `cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.46.0/dist/` |
| Alpine.js | 3.17.1 | `cdn.jsdelivr.net/npm/alpinejs@3.17.1/dist/cdn.min.js` |
| ALTCHA widget (with all translations) | altcha 3.2.2 | `registry.npmjs.org/altcha/-/altcha-3.2.2.tgz` → `dist/main/altcha.i18n.min.js` |

Licenses: MIT for all four; the license text of each library sits in its directory.

Review notes (2026-09-02): files contain no external resource loads; the only URLs
inside are license/documentation references in comments. `tabler.min.{css,js}` carry
`sourceMappingURL` comments pointing at `.map` files that are deliberately not shipped —
this only silences developer tooling, browsers ignore it.

Review notes for ALTCHA (2026-09-07): the bundle spawns its proof-of-work web
workers from embedded `data:` URLs and performs network requests only to the
challenge URL passed in by our pages; the `https://altcha.org/` string inside is
the footer attribution link (an `href`, not a resource load).

Review notes for Tabler 1.5.0 (2026-09-08): no external resource loads, no
fetch/XHR calls; the only URLs inside are license/documentation strings in
comments. Bootstrap (5.3.8) is now bundled into the Tabler source tree — there
is no separate Bootstrap dependency; `data-bs-*` attributes keep working. The
default color mode became `auto` (follows the OS), so every page pins
`data-bs-theme="light"` on `<html>` to keep the approved light look. The default
font stack is now the system one (upstream dropped the Inter reference).

SHA-256 of the vendored files:

```
4cdeade29286540dff94acfeb6ea9ea6a16bad4a64ff5604f659414b7c954cd5  tabler/tabler.min.css
0273fadc362ae4ddc8b68e9bd1fd98ae7c835b82fd9b6a4ff6ae2b7064839e55  tabler/tabler.min.js
40d8d8fdbd0dc3401cecfc069065e20268a38f58662ef64d648c7905d5033deb  tabler-icons/tabler-icons.min.css
9920d9866628db84af956877d04ff185ee3472a9716b03a9bb958b529ae1a9da  tabler-icons/fonts/tabler-icons.ttf
ed0c7bc91df578809986d98917281921c6c9e64e9a726a46caacc5b1a0967eb2  tabler-icons/fonts/tabler-icons.woff
c9df3377cc2f7b2196c57a240ff01bad34d7039abbaf7380fcfb21f6d7d8eee7  tabler-icons/fonts/tabler-icons.woff2
b30997fc126d808b1a9b20ab3f504ded88df957818c02d6249bba3ec114eb0ec  alpinejs/alpine.min.js
9e3a335795581933ff93bf768da339c4c6af093b32f67a32b0e0a4a14d822195  altcha/altcha.i18n.min.js
```
