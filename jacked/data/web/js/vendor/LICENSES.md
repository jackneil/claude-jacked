# Vendored browser scripts

The dashboard loads these files from its own origin (`/js/vendor/`), never
from a CDN. Each file is pinned by its SHA-256 in
`tests/unit/test_web_vendor_scripts.py`. To update one, download the new
version, verify it as described below, replace the file, and update the pin.

- `tailwindcss-3.4.17.js`: the Tailwind CSS Play CDN build, from
  `https://cdn.tailwindcss.com/3.4.17` (the version `cdn.tailwindcss.com`
  served on 2026-10-29). It makes no network requests.
- `sweetalert2-11.26.25.all.min.js`: `dist/sweetalert2.all.min.js` from the
  npm tarball of sweetalert2 11.26.25, checked against the registry's
  `dist.integrity` value.
- `chart.umd.min.js` (Chart.js 4.5.1) and `chartjs-chart-matrix.min.js`
  (chartjs-chart-matrix), loaded on demand by the analytics view. Both are
  MIT; each file carries its license banner (bundled in eb30ba8).

## Tailwind CSS (MIT)

```
MIT License

Copyright (c) Tailwind Labs, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## SweetAlert2 (MIT)

```
The MIT License (MIT)

Copyright (c) 2014 Tristan Edwards & Limon Monte

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

```
