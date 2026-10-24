import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { server } from "./server.ts";

before(() => {
	server.listen({ onUnhandledFrame: "error" });
});

after(() => {
	server.close();
});

test("GET /health returns ok status", async () => {
	const res = await fetch("http://localhost/health");
	assert.equal(res.status, 200);
	assert.deepEqual(await res.json(), { status: "ok" });
});

test("GET /api/config returns model_list and router_settings", async () => {
	const res = await fetch("http://localhost/api/config");
	assert.equal(res.status, 200);
	const data = (await res.json()) as {
		model_list: unknown[];
		router_settings: Record<string, unknown>;
	};
	assert.ok(Array.isArray(data.model_list));
	assert.equal(typeof data.router_settings, "object");
});

test("GET /api/config/models is rejected as unhandled", async () => {
	await assert.rejects(fetch("http://localhost/api/config/models"));
});

test("POST /api/config enforces X-Requested-With CSRF header and creates PR", async () => {
	const csrfFail = await fetch("http://localhost/api/config", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ config_yaml: "model_list: []" }),
	});
	assert.equal(csrfFail.status, 403);

	const badBody = await fetch("http://localhost/api/config", {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			"X-Requested-With": "XMLHttpRequest",
		},
		body: JSON.stringify({ config_yaml: "invalid: true" }),
	});
	assert.equal(badBody.status, 400);

	const okRes = await fetch("http://localhost/api/config", {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			"X-Requested-With": "XMLHttpRequest",
		},
		body: JSON.stringify({ config_yaml: "model_list: []" }),
	});
	assert.equal(okRes.status, 200);
	const prData = (await okRes.json()) as {
		status: string;
		url: string;
		pr_url?: string;
	};
	assert.equal(prData.status, "pr_created");
	const candidateUrl = prData.pr_url ?? prData.url;
	assert.ok(candidateUrl.startsWith("https://"));
});

test("POST /v1/chat/completions supports SSE stream and JSON completion", async () => {
	const sseRes = await fetch("http://localhost/v1/chat/completions", {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			"X-Requested-With": "XMLHttpRequest",
		},
		body: JSON.stringify({
			messages: [{ role: "user", content: "hello" }],
			stream: true,
		}),
	});
	assert.equal(sseRes.status, 200);
	assert.match(sseRes.headers.get("content-type") ?? "", /text\/event-stream/);
	const sseText = await sseRes.text();
	let assembled = "";
	for (const line of sseText.split("\n")) {
		if (line.startsWith("data: ") && line !== "data: [DONE]") {
			const parsed = JSON.parse(line.slice(6)) as {
				choices?: Array<{ delta?: { content?: string } }>;
			};
			if (parsed.choices?.[0]?.delta?.content) {
				assembled += parsed.choices[0].delta.content;
			}
		}
	}
	assert.equal(assembled, "Mock response");

	const jsonRes = await fetch("http://localhost/v1/chat/completions", {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			"X-Requested-With": "XMLHttpRequest",
		},
		body: JSON.stringify({
			messages: [{ role: "user", content: "hello" }],
			stream: false,
		}),
	});
	assert.equal(jsonRes.status, 200);
	const jsonData = (await jsonRes.json()) as {
		choices: Array<{ message: { content: string } }>;
	};
	assert.equal(jsonData.choices[0]?.message.content, "Mock response");
});
