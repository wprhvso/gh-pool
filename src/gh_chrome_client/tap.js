(() => {
  if (window.__ghTap) return true;

  const MAX_READ = 262144;

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

  const keep = (rule, entry) => {
    const wake = waiting.get(rule.name);
    if (wake) {
      waiting.delete(rule.name);
      wake(entry);
      return;
    }
    taken.set(rule.name, entry);
  };

  const take = (name, waitMs) => {
    const entry = taken.get(name);
    if (entry) {
      taken.delete(name);
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

  const contentType = (rule) => rule.headers["content-type"] || "application/json";

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
    return null;
  };

  const headersOf = (headers) => {
    const plain = {};
    headers.forEach((value, key) => {
      plain[key] = value;
    });
    return plain;
  };

  const canned = (rule) =>
    new Response(rule.body ?? "", {
      status: rule.status,
      headers: { "content-type": contentType(rule), ...rule.headers },
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
        body: rule.body,
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

  const settle = (xhr, rule) => {
    const body = rule.body ?? "";
    const type = contentType(rule);
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
    xhr.getAllResponseHeaders = () => `content-type: ${type}\r\n`;
    xhr.getResponseHeader = (name) =>
      String(name).toLowerCase() === "content-type" ? type : null;
    setTimeout(() => {
      xhr.dispatchEvent(new Event("readystatechange"));
      xhr.dispatchEvent(new ProgressEvent("load"));
      xhr.dispatchEvent(new ProgressEvent("loadend"));
    }, 0);
  };

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
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
      keep(rule, {
        name: rule.name,
        url: call.url,
        method: call.method,
        headers: call.headers,
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
        body: request.body,
        credentials: "include",
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
        if (step.done) break;
        stream.text += decoder.decode(step.value, { stream: true });
        notify(stream);
      }
    } catch (failure) {
      stream.error = String(failure);
    } finally {
      stream.done = true;
      notify(stream);
    }
  };

  const replay = (request) => {
    const id = String(++counter);
    const stream = { text: "", status: 0, error: null, done: false, wake: null };
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

  window.__ghTap = {
    configure: (next) => {
      rules.length = 0;
      rules.push(...next);
      taken.clear();
      return true;
    },
    take,
    replay,
    read,
    stop: (id) => streams.delete(id),
  };

  return true;
})()
