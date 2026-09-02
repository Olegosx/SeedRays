# Vendored libraries

Policy: every third-party library is a reviewed, fixed-version file stored in this
directory with its license next to it; no CDNs in production; updating a library is a
deliberate separate task (see ADR-0012).

| Library | Version | Source (pinned) |
|---------|---------|-----------------|
| Tabler | @tabler/core 1.4.0 | `cdn.jsdelivr.net/npm/@tabler/core@1.4.0/dist/` |
| Tabler Icons (webfont) | 3.46.0 | `cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.46.0/dist/` |
| Alpine.js | 3.17.1 | `cdn.jsdelivr.net/npm/alpinejs@3.17.1/dist/cdn.min.js` |

Licenses: MIT for all three; the license text of each library sits in its directory.

Review notes (2026-09-02): files contain no external resource loads; the only URLs
inside are license/documentation references in comments. `tabler.min.{css,js}` carry
`sourceMappingURL` comments pointing at `.map` files that are deliberately not shipped —
this only silences developer tooling, browsers ignore it.

SHA-256 of the vendored files:

```
7ef750bd10546a695d0b12767ad8048bd8f3ec5de7daefb1067f9d0daa3d1c9a  tabler/tabler.min.css
b60c76160e97624574dbb8cf10abe6aee9a6493b60096fdfc15dd1dd2bd99eb9  tabler/tabler.min.js
40d8d8fdbd0dc3401cecfc069065e20268a38f58662ef64d648c7905d5033deb  tabler-icons/tabler-icons.min.css
9920d9866628db84af956877d04ff185ee3472a9716b03a9bb958b529ae1a9da  tabler-icons/fonts/tabler-icons.ttf
ed0c7bc91df578809986d98917281921c6c9e64e9a726a46caacc5b1a0967eb2  tabler-icons/fonts/tabler-icons.woff
c9df3377cc2f7b2196c57a240ff01bad34d7039abbaf7380fcfb21f6d7d8eee7  tabler-icons/fonts/tabler-icons.woff2
b30997fc126d808b1a9b20ab3f504ded88df957818c02d6249bba3ec114eb0ec  alpinejs/alpine.min.js
```
