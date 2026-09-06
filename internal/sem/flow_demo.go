package sem

// summarizeFlowParameters is a thin convenience wrapper for callers that only
// need a symbol's flow parameter names without touching flow resolution
// directly.
func summarizeFlowParameters(symbol SymbolRecord) map[string]bool {
	return symbolFlowParameterNames(symbol)
}
