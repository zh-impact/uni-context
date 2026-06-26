package app

import (
	"fmt"
	"io"

	"uni-context/internal/adapter/pdf"
	"uni-context/internal/config"
	"uni-context/internal/port"
)

// BuildPDFExtractor returns the default extractor per cfg.Engine.
// Returns (nil, nil) when PDF is unconfigured — caller proceeds
// without PDF support, and IngestService.Create errors clearly if a
// PDF is passed (see service package).
func BuildPDFExtractor(cfg config.PDFConfig, log io.Writer) (port.PDFExtractor, error) {
	if cfg.Engine == "" {
		return nil, nil
	}
	return buildExtractor(cfg.Engine, cfg.Engines, log)
}

// BuildExtractorForEngine returns an extractor for an explicit engine
// name. Used by the CLI when --engine overrides the config default.
// Errors name the specific config key the user must set when the
// chosen engine lacks required config.
func BuildExtractorForEngine(name string, cfg config.PDFConfig, log io.Writer) (port.PDFExtractor, error) {
	return buildExtractor(name, cfg.Engines, log)
}

func buildExtractor(name string, engines map[string]config.EngineConfig, log io.Writer) (port.PDFExtractor, error) {
	switch name {
	case "gxpdf":
		return pdf.NewGxpdfExtractor(log), nil
	case "shell":
		ec, ok := engines["shell"]
		if !ok || ec.Command == "" {
			return nil, fmt.Errorf(
				"engine %q not configured (set pdf.engines.shell.command in config.yaml)", name)
		}
		return pdf.NewShellExtractor(ec.Command, ec.Timeout), nil
	case "http":
		ec, ok := engines["http"]
		if !ok || ec.URL == "" {
			return nil, fmt.Errorf(
				"engine %q not configured (set pdf.engines.http.url in config.yaml)", name)
		}
		return pdf.NewHttpExtractor(ec.URL, ec.Timeout, ec.AuthToken), nil
	default:
		return nil, fmt.Errorf(
			"unknown pdf engine %q (want gxpdf|shell|http)", name)
	}
}
