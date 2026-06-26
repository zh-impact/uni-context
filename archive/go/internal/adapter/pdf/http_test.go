package pdf

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestHttpExtractor_POSTsBinaryAndReturnsTextBody(t *testing.T) {
	var (
		gotPath      string
		gotMethod    string
		gotContent   string
		gotAuth      string
		gotBodyBytes []byte
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotMethod = r.Method
		gotContent = r.Header.Get("Content-Type")
		gotAuth = r.Header.Get("Authorization")
		gotBodyBytes = make([]byte, 1024)
		n, _ := r.Body.Read(gotBodyBytes)
		gotBodyBytes = gotBodyBytes[:n]
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = w.Write([]byte("server extracted this text"))
	}))
	defer srv.Close()

	x := NewHttpExtractor(srv.URL+"/extract", 5*time.Second, "tok-abc")
	text, err := x.Extract(context.Background(), []byte("%PDF-1.4 fake bytes"))
	require.NoError(t, err)
	assert.Equal(t, "server extracted this text", text)

	assert.Equal(t, "/extract", gotPath)
	assert.Equal(t, http.MethodPost, gotMethod)
	assert.Equal(t, "application/pdf", gotContent)
	assert.Equal(t, "Bearer tok-abc", gotAuth)
	assert.Equal(t, "%PDF-1.4 fake bytes", string(gotBodyBytes))
}

func TestHttpExtractor_ErrorsOnNon2xx(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte("malformed pdf body"))
	}))
	defer srv.Close()

	x := NewHttpExtractor(srv.URL, 5*time.Second, "")
	_, err := x.Extract(context.Background(), []byte("fake"))
	require.Error(t, err)
	s := err.Error()
	assert.Contains(t, s, "422", "error must mention status code")
	assert.Contains(t, s, "malformed pdf body", "error must include body snippet")
}

func TestHttpExtractor_ErrorsOnWrongResponseMIME(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"text":"hi"}`))
	}))
	defer srv.Close()

	x := NewHttpExtractor(srv.URL, 5*time.Second, "")
	_, err := x.Extract(context.Background(), []byte("fake"))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "text/plain",
		"error must mention expected MIME")
}

func TestHttpExtractor_TimesOut(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(2 * time.Second)
	}))
	defer srv.Close()

	x := NewHttpExtractor(srv.URL, 100*time.Millisecond, "")
	_, err := x.Extract(context.Background(), []byte("fake"))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "timeout")
}

func TestHttpExtractor_OmitsAuthHeaderWhenTokenEmpty(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "text/plain")
	}))
	defer srv.Close()

	x := NewHttpExtractor(srv.URL, 5*time.Second, "")
	_, _ = x.Extract(context.Background(), []byte("fake"))
	assert.Empty(t, gotAuth, "no Authorization header when token empty")
}
