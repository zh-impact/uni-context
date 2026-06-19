VERSION ?= dev
BIN     ?= unictx
PKG     := ./...

.PHONY: build test test-race lint fmt vet clean install

build:
	CGO_ENABLED=1 go build -tags sqlite_fts5 -ldflags "-X main.version=$(VERSION)" -o $(BIN) ./cmd/unictx

test:
	CGO_ENABLED=1 go test -tags 'sqlite_fts5' $(PKG)

test-race:
	CGO_ENABLED=1 go test -race -tags 'sqlite_fts5' $(PKG)

test-integration:
	CGO_ENABLED=1 go test -tags 'integration,sqlite_fts5' $(PKG)

fmt:
	gofmt -s -w .

vet:
	go vet $(PKG)

lint: vet
	@command -v golangci-lint >/dev/null 2>&1 && golangci-lint run || echo "golangci-lint not installed; skipping"

clean:
	rm -f $(BIN) coverage.txt
	go clean -testcache

install: build
	mv $(BIN) $(GOPATH)/bin/$(BIN)
