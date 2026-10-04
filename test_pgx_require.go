package main

import (
	"fmt"
	"github.com/jackc/pgx/v5"
)

func main() {
	cfg, err := pgx.ParseConfig("postgres://user:pass@host:5432/db?sslmode=require")
	if err != nil {
		fmt.Println("Error:", err)
		return
	}
	fmt.Printf("TLSConfig present: %v\n", cfg.TLSConfig != nil)
	if cfg.TLSConfig != nil {
		fmt.Printf("InsecureSkipVerify: %v\n", cfg.TLSConfig.InsecureSkipVerify)
	}
}
