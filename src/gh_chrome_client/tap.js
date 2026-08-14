(() => {
  if (window.__ghTap) return true;

  const MAX_READ = 262144;
  // What one replay may hold for a client that has stopped reading it. Past
  // this the page is buffering a response nobody is taking, and the tab pays
  // for it: the stream is failed instead.
  const MAX_BUFFER = 8388608;

  const origin = {
    fetch: window.fetch.bind(window),
    open: XMLHttpRequest.prototype.open,
    send: XMLHttpRequest.prototype.send,
    header: XMLHttpRequest.prototype.setRequestHeader,
  };

  const rules = [];
  const taken = new Map();
  const waiting = new Map();
  const streams = new Map();
  let counter = 0;

  const absolute = (url) => {
    try {
      return new URL(url, document.baseURI).href;
    } catch {
      return String(url);
    }
  };

  const ruleFor = (url, method) =>
    rules.find(
      (rule) => url.includes(rule.url) && (!rule.method || rule.method === method),
    ) || null;

  // Two requests can match one rule before the client comes to collect, and the
  // second used to overwrite the first — both kept off the network, only one
  // ever handed over.
  const keep = (rule, entry) => {
    const wake = waiting.get(rule.name);
    if (wake) {
      waiting.delete(rule.name);
      wake(entry);
      return;
    }
    const queue = taken.get(rule.name);
    if (queue) queue.push(entry);
    else taken.set(rule.name, [entry]);
  };

  const take = (name, waitMs) => {
    const queue = taken.get(name);
    if (queue && queue.length) {
      const entry = queue.shift();
      if (!queue.length) taken.delete(name);
      return Promise.resolve(entry);
    }
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        waiting.delete(name);
        resolve(null);
      }, waitMs);
      waiting.set(name, (value) => {
        clearTimeout(timer);
        resolve(value);
      });
    });
  };

  // The rule is written in Python, where a header may be spelled any way at
  // all; everything below reads them in lower case, and the Headers
  // constructor appends rather than replaces, so a "Content-Type" next to a
  // "content-type" would make the response's type both of them at once.
  const lowerKeys = (headers) => {
    const plain = {};
    for (const [key, value] of Object.entries(headers || {})) {
      plain[String(key).toLowerCase()] = String(value);
    }
    return plain;
  };

  const contentType = (rule) => rule.headers["content-type"] || "application/json";

  const answerHeaders = (rule) => ({
    "content-type": contentType(rule),
    ...rule.headers,
  });

  const parse = (text) => {
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  };

  const asText = (body) => {
    if (body == null) return null;
    if (typeof body === "string") return body;
    if (body instanceof URLSearchParams) return body.toString();
    if (typeof Document !== "undefined" && body instanceof Document) {
      return new XMLSerializer().serializeToString(body);
    }
    if (body instanceof ArrayBuffer) return new TextDecoder().decode(body);
    if (ArrayBuffer.isView(body)) {
      return new TextDecoder().decode(
        new Uint8Array(body.buffer, body.byteOffset, body.byteLength),
      );
    }
    return null;
  };

  // What XHR puts on the wire when the page set no content type of its own.
  // Without it a captured request replays as something the server reads
  // differently from what the page actually sent.
  const impliedType = (body) => {
    if (body == null) return null;
    if (typeof body === "string") return "text/plain;charset=UTF-8";
    if (body instanceof URLSearchParams) {
      return "application/x-www-form-urlencoded;charset=UTF-8";
    }
    if (typeof Document !== "undefined" && body instanceof Document) {
      return "text/html;charset=UTF-8";
    }
    if (typeof Blob !== "undefined" && body instanceof Blob) return body.type || null;
    return null;
  };

  const headersOf = (headers) => {
    const plain = {};
    headers.forEach((value, key) => {
      plain[key] = value;
    });
    return plain;
  };

  // A GET or a HEAD carries no body, and fetch throws rather than ignoring one:
  // a captured GET keeps an empty string where a body would be.
  const sendable = (method, body) =>
    body && method !== "GET" && method !== "HEAD" ? body : undefined;

  const canned = (rule) =>
    new Response(rule.body ?? "", {
      status: rule.status,
      headers: answerHeaders(rule),
    });

  window.fetch = async (input, init) => {
    let request;
    try {
      request = new Request(input, init);
    } catch {
      return origin.fetch(input, init);
    }
    const rule = ruleFor(request.url, request.method);
    if (!rule) return origin.fetch(input, init);
    if (rule.action === "rewrite") {
      return origin.fetch(request.url, {
        method: request.method,
        headers: request.headers,
        body: sendable(request.method, rule.body),
        credentials: "include",
      });
    }
    if (rule.action === "capture") {
      const body = await request
        .clone()
        .text()
        .catch(() => null);
      keep(rule, {
        name: rule.name,
        url: request.url,
        method: request.method,
        headers: headersOf(request.headers),
        body,
      });
    }
    return canned(rule);
  };

  const SHADOWED = [
    "readyState",
    "status",
    "statusText",
    "responseText",
    "responseURL",
    "response",
  ];

  // Reusing an XMLHttpRequest is ordinary in polling code, and the own getters
  // installed by settle would otherwise shadow every real response the object
  // ever gets afterwards.
  const unsettle = (xhr) => {
    if (!xhr.__ghTapSettled) return;
    for (const key of SHADOWED) delete xhr[key];
    delete xhr.getAllResponseHeaders;
    delete xhr.getResponseHeader;
    xhr.__ghTapSettled = false;
  };

  const settle = (xhr, rule) => {
    const body = rule.body ?? "";
    const headers = answerHeaders(rule);
    const values = {
      readyState: 4,
      status: rule.status,
      statusText: "",
      responseText: body,
      responseURL: "",
      response: xhr.responseType === "json" ? parse(body) : body,
    };
    for (const [key, value] of Object.entries(values)) {
      Object.defineProperty(xhr, key, { configurable: true, get: () => value });
    }
    const lines = Object.entries(headers)
      .map(([name, value]) => `${name}: ${value}\r\n`)
      .join("");
    xhr.getAllResponseHeaders = () => lines;
    xhr.getResponseHeader = (name) => headers[String(name).toLowerCase()] ?? null;
    xhr.__ghTapSettled = true;
    setTimeout(() => {
      xhr.dispatchEvent(new Event("readystatechange"));
      xhr.dispatchEvent(new ProgressEvent("load"));
      xhr.dispatchEvent(new ProgressEvent("loadend"));
    }, 0);
  };

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    unsettle(this);
    this.__ghTapCall = {
      method: String(method).toUpperCase(),
      url: absolute(url),
      headers: {},
    };
    return origin.open.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    const call = this.__ghTapCall;
    if (call) call.headers[String(name).toLowerCase()] = String(value);
    return origin.header.call(this, name, value);
  };

  XMLHttpRequest.prototype.send = function (body) {
    const call = this.__ghTapCall;
    const rule = call ? ruleFor(call.url, call.method) : null;
    if (!rule) return origin.send.call(this, body);
    if (rule.action === "rewrite") return origin.send.call(this, rule.body);
    if (rule.action === "capture") {
      const headers = { ...call.headers };
      const implied = impliedType(body);
      if (!headers["content-type"] && implied) headers["content-type"] = implied;
      keep(rule, {
        name: rule.name,
        url: call.url,
        method: call.method,
        headers,
        body: asText(body),
      });
    }
    return settle(this, rule);
  };

  const cut = (stream) => {
    const text = stream.text.slice(0, MAX_READ);
    stream.text = stream.text.slice(text.length);
    return {
      text,
      status: stream.status,
      error: stream.error,
      done: stream.done && !stream.text,
    };
  };

  const notify = (stream) => {
    const wake = stream.wake;
    if (wake) {
      stream.wake = null;
      wake();
    }
  };

  const pump = async (stream, request) => {
    try {
      const response = await origin.fetch(request.url, {
        method: request.method,
        headers: request.headers,
        body: sendable(request.method, request.body),
        credentials: "include",
        signal: stream.control.signal,
      });
      stream.status = response.status;
      if (!response.body) {
        stream.text += await response.text();
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      for (;;) {
        const step = await reader.read();
        if (step.done || stream.stopped) break;
        stream.text += decoder.decode(step.value, { stream: true });
        if (stream.text.length > MAX_BUFFER) {
          throw new Error("the replayed response outgrew the buffer");
        }
        notify(stream);
      }
      if (stream.stopped) await reader.cancel().catch(() => {});
    } catch (failure) {
      stream.error = String(failure);
    } finally {
      stream.done = true;
      notify(stream);
    }
  };

  const replay = (request) => {
    const id = String(++counter);
    const stream = {
      text: "",
      status: 0,
      error: null,
      done: false,
      wake: null,
      stopped: false,
      control: new AbortController(),
    };
    streams.set(id, stream);
    void pump(stream, request);
    return id;
  };

  const read = (id, waitMs) => {
    const stream = streams.get(id);
    if (!stream) {
      return Promise.resolve({
        text: "",
        status: 0,
        error: "unknown stream",
        done: true,
      });
    }
    if (stream.text || stream.done) return Promise.resolve(cut(stream));
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        stream.wake = null;
        resolve(cut(stream));
      }, waitMs);
      stream.wake = () => {
        clearTimeout(timer);
        resolve(cut(stream));
      };
    });
  };

  // Forgetting the id is not enough: the pump holds the stream object itself
  // and would go on fetching and buffering, for the life of the document, a
  // response nobody can read any more.
  const stop = (id) => {
    const stream = streams.get(id);
    if (!stream) return false;
    stream.stopped = true;
    stream.control.abort();
    streams.delete(id);
    return true;
  };

  window.__ghTap = {
    configure: (next) => {
      rules.length = 0;
      rules.push(
        ...next.map((rule) => ({ ...rule, headers: lowerKeys(rule.headers) })),
      );
      taken.clear();
      return true;
    },
    take,
    replay,
    read,
    stop,
  };

  return true;
})()
