import type { ChatSource, ChatStreamEvent } from "./contracts";

export type ChatStreamState = {
  content: string;
  sources: ChatSource[];
  stage: string;
  completed: boolean;
};

export function createChatStreamState(): ChatStreamState {
  return { content: "", sources: [], stage: "正在处理…", completed: false };
}

export function applyChatStreamEvent(
  state: ChatStreamState,
  event: ChatStreamEvent,
): ChatStreamState {
  switch (event.type) {
    case "stage":
      return { ...state, stage: event.message };
    case "sources":
      return { ...state, sources: event.sources };
    case "delta":
      return { ...state, content: state.content + event.text };
    case "done":
      return { ...state, completed: true };
    case "error":
      throw new Error(event.message || "回答失败");
  }
}

export function visibleChatStreamContent(state: ChatStreamState) {
  return state.content || state.stage;
}
