# PDF test fixtures

Generated 2026-06-26. Regeneration requires `qpdf` (any recent version) and a
one-off Go program that uses `github.com/coregx/gxpdf/creator` to author the
two unencrypted PDFs; the encrypted variant is produced from `sample.pdf`
via `qpdf --encrypt`.

## Contracts (asserted by `gxpdf_test.go`)

- `sample.pdf`: 1 page whose body text contains the phrase
  `"the quick brown fox"`. ~760 bytes.
- `blank.pdf`: 1 page with no text drawn. gxpdf's `ExtractText()` on this
  file returns `""`.
- `encrypted.pdf`: password-protected (RC4-40, V=1, R=2); user password
  `"user"`, owner password `"owner"`. Opening without a password fails;
  gxpdf returns an error containing `"encrypted"`.

## How to regenerate

### sample.pdf and blank.pdf

Save as `/tmp/pdfgen/main.go` and run inside a module whose `go.mod`
requires `github.com/coregx/gxpdf` at the version pinned in this
repo's `go.mod`:

```go
package main

import (
	"github.com/coregx/gxpdf/creator"
)

func main() {
	// sample.pdf
	c := creator.New()
	p, _ := c.NewPage()
	_ = p.AddText("the quick brown fox jumps over the lazy dog",
		72, 700, creator.Helvetica, 14)
	_ = c.WriteToFile("sample.pdf")

	// blank.pdf
	b := creator.New()
	_, _ = b.NewPage()
	_ = b.WriteToFile("blank.pdf")
}
```

### encrypted.pdf

Requires `qpdf` 12.x. `qpdf` refuses RC4 by default; pass
`--allow-weak-crypto` because gxpdf's `security.NewDecryptor` returns
`ErrUnsupportedVersion` for AES-256 (V=5), which would surface a
different error message. RC4-40 produces the cleanest "encrypted PDF:
password required" error path:

```
qpdf --allow-weak-crypto --encrypt user owner 40 -- \
    sample.pdf encrypted.pdf
```

Verify:

```
qpdf --requires-password encrypted.pdf   # exit 0
```

## Tools used

- `github.com/coregx/gxpdf/creator` v0.8.2 (matched `go.mod`) — authoring
- `qpdf` 12.3.2 — encryption
