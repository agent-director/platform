import { HttpResponse, http } from "msw";

if (typeof globalThis.location === "undefined") {
	Object.defineProperty(globalThis, "location", {
		value: new URL("http://localhost"),
		configurable: true,
		writable: true,
	});
}

export interface HealthContract {
	status: "ok";
}

export interface ModelConfigContract {
	model_list: Array<{ model_name: string }>;
	router_settings: Record<string, unknown>;
}

export interface ConfigUpdateRequestContract {
	config_yaml: string;
}

export interface ConfigUpdateResponseContract {
	status: "pr_created";
	url: string;
	pr_url?: string;
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
	http.post("/api/config", async ({ request }) => {
		if (request.headers.get("X-Requested-With") !== "XMLHttpRequest") {
			return HttpResponse.json(
				{ detail: "CSRF verification failed: Missing X-Requested-With header" },
				{ status: 403 },
			);
		}
		const body = (await request
			.json()
			.catch(() => null)) as Partial<ConfigUpdateRequestContract> | null;
		if (
			!body ||
			typeof body.config_yaml !== "string" ||
			!body.config_yaml.includes("model_list")
		) {
			return HttpResponse.json(
				{
					detail:
						"Invalid YAML provided: Configuration must contain a valid 'model_list'",
				},
				{ status: 400 },
			);
		}
		return HttpResponse.json<ConfigUpdateResponseContract>({
			status: "pr_created",
			url: "https://github.com/agent-director/platform/pull/1",
		});
	}),
	http.post("/v1/chat/completions", async ({ request }) => {
		if (request.headers.get("X-Requested-With") !== "XMLHttpRequest") {
			return HttpResponse.json(
				{ detail: "CSRF verification failed: Missing X-Requested-With header" },
				{ status: 403 },
			);
		}
		const body = (await request.json().catch(() => ({}))) as {
			stream?: boolean;
		};
		if (body.stream === false) {
			return HttpResponse.json({
				id: "chatcmpl-mock",
				object: "chat.completion",
				choices: [
					{
						index: 0,
						message: { role: "assistant", content: "Mock response" },
						finish_reason: "stop",
					},
				],
			});
		}
		const ssePayload = [
			`data: ${JSON.stringify({
				id: "chatcmpl-mock",
				object: "chat.completion.chunk",
				choices: [
					{
						index: 0,
						delta: { role: "assistant", content: "Mock response" },
						finish_reason: "stop",
					},
				],
			})}`,
			"",
			"data: [DONE]",
			"",
			"",
		].join("\n");
		return new HttpResponse(ssePayload, {
			headers: { "Content-Type": "text/event-stream" },
		});
	}),
];
