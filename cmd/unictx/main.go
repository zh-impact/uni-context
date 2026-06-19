package main

import (
    "fmt"
    "os"
)

var version = "dev"

func main() {
    if len(os.Args) > 1 && os.Args[1] == "--version" {
        fmt.Println(version)
        return
    }
    fmt.Fprintln(os.Stderr, "uni-context", version, "(skeleton — see Plan 1 task 11+)")
    os.Exit(1)
}
