import { HttpResponse, http } from "msw";

export interface HealthContract {
	status: "ok";
}

export interface ModelConfigContract {
	model_list: Array<{ model_name: string }>;
	router_settings: Record<string, unknown>;
}

export const handlers = [
	http.get("/health", () =>
		HttpResponse.json<HealthContract>({ status: "ok" }),
	),
	http.get("/api/config", () =>
		HttpResponse.json<ModelConfigContract>({
			model_list: [{ model_name: "claude-sonnet-5-5" }],
			router_settings: {},
		}),
	),
	http.get("/api/config/models", () =>
		HttpResponse.json<ModelConfigContract>({
			model_list: [{ model_name: "claude-sonnet-5-5" }],
			router_settings: {},
		}),
	),
	http.post("/v1/chat/completions", () =>
		HttpResponse.json({
			id: "chatcmpl-mock",
			object: "chat.completion",
			choices: [
				{
					index: 0,
					message: { role: "assistant", content: "Mock response" },
					finish_reason: "stop",
				},
			],
		}),
	),
];
