"use client";

import { useEffect, useState } from "react";

interface WorkflowSummary {
	id: string;
	state: string;
}

export default function Dashboard() {
	const [configInput, setConfigInput] = useState("");
	const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
	const [chatInput, setChatInput] = useState("");
	const [chatLog, setChatLog] = useState("");
	const [streamingMessage, setStreamingMessage] = useState("");
	const [prUrl, setPrUrl] = useState("");

	useEffect(() => {
		const fetchConfig = async () => {
			try {
				const res = await fetch("/api/config");
				const data = await res.json();
				setConfigInput(JSON.stringify(data, null, 2));
			} catch (e) {
				console.error("Failed to fetch config", e);
			}
		};
		fetchConfig();
		setWorkflows([{ id: "wf_12345", state: "running" }]);
	}, []);

	const updateConfig = async () => {
		try {
			// JSON is valid YAML; send raw string to backend for parsing
			const res = await fetch("/api/config", {
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Requested-With": "XMLHttpRequest", // Required for CSRF mitigation
				},
				body: JSON.stringify({ config_yaml: configInput }),
			});
			if (res.ok) {
				const data = await res.json();
				const candidateUrl = data.pr_url ?? data.url;
				if (
					typeof candidateUrl === "string" &&
					(candidateUrl.startsWith("https://") ||
						candidateUrl.startsWith("http://"))
				) {
					setPrUrl(candidateUrl);
				}
			} else {
				alert(`Failed to submit PR: ${await res.text()}`);
			}
		} catch (e) {
			console.error("Failed to update config", e);
		}
	};

	const sendChat = async () => {
		if (!chatInput) return;
		setChatLog((prev) => `${prev}\nUser: ${chatInput}\nAssistant: `);
		setStreamingMessage("");

		const payload = {
			messages: [{ role: "user", content: chatInput }],
			stream: true,
		};
		setChatInput("");

		try {
			const res = await fetch("/v1/chat/completions", {
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Requested-With": "XMLHttpRequest",
				},
				body: JSON.stringify(payload),
			});

			const reader = res.body?.getReader();
			if (!reader) throw new Error("Response body is not readable");
			const decoder = new TextDecoder();
			let buffer = "";
			let localStreamingMessage = "";

			while (true) {
				const { done, value } = await reader.read();
				if (done) break;
				const chunk = decoder.decode(value);
				buffer += chunk;

				const lines = buffer.split("\n");
				buffer = lines.pop() || ""; // Keep the incomplete line in the buffer

				for (const line of lines) {
					if (line.startsWith("data: ") && line !== "data: [DONE]") {
						try {
							const data = JSON.parse(line.slice(6));
							if (data.choices?.[0]?.delta?.content) {
								localStreamingMessage += data.choices[0].delta.content;
								setStreamingMessage(localStreamingMessage);
							}
						} catch {
							// Ignore parse errors from partial JSON if any
						}
					}
				}
			}
			setChatLog((prev) => `${prev}${localStreamingMessage}\n`);
			setStreamingMessage("");
		} catch (e) {
			setChatLog((prev) => `${prev}[Error: ${e}]\n`);
		}
	};

	return (
		<div
			style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "2rem" }}
		>
			<div>
				<section
					style={{
						backgroundColor: "white",
						padding: "1.5rem",
						borderRadius: "8px",
						boxShadow: "0 1px 3px rgba(0,0,0,0.1)",
						marginBottom: "2rem",
					}}
				>
					<h2>Workflows</h2>
					<ul style={{ listStyleType: "none", padding: 0 }}>
						{workflows.map((wf) => (
							<li
								key={wf.id}
								style={{
									padding: "0.5rem 0",
									borderBottom: "1px solid #e5e7eb",
								}}
							>
								<strong>{wf.id}</strong> - Status:{" "}
								<span
									style={{ color: wf.state === "running" ? "green" : "gray" }}
								>
									{wf.state}
								</span>
							</li>
						))}
					</ul>
				</section>

				<section
					style={{
						backgroundColor: "white",
						padding: "1.5rem",
						borderRadius: "8px",
						boxShadow: "0 1px 3px rgba(0,0,0,0.1)",
					}}
				>
					<textarea
						style={{
							width: "100%",
							height: "300px",
							backgroundColor: "#f3f4f6",
							padding: "1rem",
							borderRadius: "4px",
							border: "1px solid #ccc",
							fontFamily: "monospace",
						}}
						value={configInput}
						onChange={(e) => setConfigInput(e.target.value)}
					/>
					<button
						type="button"
						onClick={updateConfig}
						style={{
							marginTop: "1rem",
							padding: "0.5rem 1rem",
							backgroundColor: "#3b82f6",
							color: "white",
							border: "none",
							borderRadius: "4px",
							cursor: "pointer",
						}}
					>
						Submit Config PR
					</button>
					{prUrl && (
						<p style={{ marginTop: "1rem", color: "green" }}>
							PR Created:{" "}
							<a href={prUrl} target="_blank" rel="noopener noreferrer">
								{prUrl}
							</a>
						</p>
					)}
				</section>
			</div>

			<div>
				<section
					style={{
						backgroundColor: "white",
						padding: "1.5rem",
						borderRadius: "8px",
						boxShadow: "0 1px 3px rgba(0,0,0,0.1)",
						height: "100%",
						display: "flex",
						flexDirection: "column",
					}}
				>
					<h2>Interactive Chat</h2>
					<pre
						style={{
							flex: 1,
							backgroundColor: "#f3f4f6",
							padding: "1rem",
							borderRadius: "4px",
							whiteSpace: "pre-wrap",
							overflowY: "auto",
							minHeight: "300px",
						}}
					>
						{chatLog}
						{streamingMessage}
					</pre>
					<div style={{ display: "flex", gap: "0.5rem", marginTop: "1rem" }}>
						<input
							type="text"
							value={chatInput}
							onChange={(e) => setChatInput(e.target.value)}
							onKeyDown={(e) => e.key === "Enter" && sendChat()}
							placeholder="Type a message..."
							style={{
								flex: 1,
								padding: "0.5rem",
								border: "1px solid #d1d5db",
								borderRadius: "4px",
							}}
						/>
						<button
							type="button"
							onClick={sendChat}
							style={{
								padding: "0.5rem 1rem",
								backgroundColor: "#10b981",
								color: "white",
								border: "none",
								borderRadius: "4px",
								cursor: "pointer",
							}}
						>
							Send
						</button>
					</div>
				</section>
			</div>
		</div>
	);
}
