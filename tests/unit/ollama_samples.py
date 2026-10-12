"""Ollama v0.40.3 / qwen3.5:0.8b probe bytes, collected 2026-10-12.

Names identify the original probe/*.json request/response pairs.
"""

import base64

from wiretruth.formats import Body, Exchange
from wiretruth.recorder import Response, response_body

PROBES = {
    "n-text": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "Write a 200-word paragraph about how octopuses change colour"
                        " to hide from predators."
                    ),
                }
            ],
            "stream": False,
            "think": False,
            "options": {"num_predict": 16},
        },
        "status": 200,
        "content_type": "application/json; charset=utf-8",
        ("body"): (
            '{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:45:34.47'
            '8295016Z","message":{"role":"assistant","content":"Octopuses'
            " possess remarkable color-changing abilities, evolving disti"
            'nct hues to camouflage against the"},"done":true,"done_reaso'
            'n":"length","total_duration":9995067888,"load_duration":4702'
            '238709,"prompt_eval_count":32,"prompt_eval_cached_count":0,"'
            'prompt_eval_duration":865446000,"eval_count":16,"eval_durati'
            'on":4413289000}'
        ),
    },
    "n-text-stream": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "Write a 200-word paragraph about how octopuses change colour"
                        " to hide from predators."
                    ),
                }
            ],
            "stream": True,
            "think": False,
            "options": {"num_predict": 16},
        },
        "status": 200,
        "content_type": "application/x-ndjson",
        ("body"): (
            '{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:45:34.64'
            '1419381Z","message":{"role":"assistant","content":"The"},"do'
            'ne":false}\n{"model":"qwen3.5:0.8b","created_at":"2026-10-12T'
            '17:45:34.973038369Z","message":{"role":"assistant","content"'
            ':" vibrant"},"done":false}\n{"model":"qwen3.5:0.8b","created_'
            'at":"2026-10-12T17:45:35.365229102Z","message":{"role":"assi'
            'stant","content":" world"},"done":false}\n{"model":"qwen3.5:0'
            '.8b","created_at":"2026-10-12T17:45:35.914788Z","message":{"'
            'role":"assistant","content":" of"},"done":false}\n{"model":"q'
            'wen3.5:0.8b","created_at":"2026-10-12T17:45:35.915061062Z","'
            'message":{"role":"assistant","content":" marine"},"done":fal'
            'se}\n{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:45:3'
            '6.456082343Z","message":{"role":"assistant","content":" life'
            '"},"done":false}\n{"model":"qwen3.5:0.8b","created_at":"2026-'
            '10-12T17:45:36.456347765Z","message":{"role":"assistant","co'
            'ntent":" is"},"done":false}\n{"model":"qwen3.5:0.8b","created'
            '_at":"2026-10-12T17:45:36.73137855Z","message":{"role":"assi'
            'stant","content":" filled"},"done":false}\n{"model":"qwen3.5:'
            '0.8b","created_at":"2026-10-12T17:45:37.083001264Z","message'
            '":{"role":"assistant","content":" with"},"done":false}\n{"mod'
            'el":"qwen3.5:0.8b","created_at":"2026-10-12T17:45:37.0832706'
            '86Z","message":{"role":"assistant","content":" creatures"},"'
            'done":false}\n{"model":"qwen3.5:0.8b","created_at":"2026-10-1'
            '2T17:45:37.492731091Z","message":{"role":"assistant","conten'
            't":" that"},"done":false}\n{"model":"qwen3.5:0.8b","created_a'
            't":"2026-10-12T17:45:37.492748286Z","message":{"role":"assis'
            'tant","content":" have"},"done":false}\n{"model":"qwen3.5:0.8'
            'b","created_at":"2026-10-12T17:45:37.946928269Z","message":{'
            '"role":"assistant","content":" mastered"},"done":false}\n{"mo'
            'del":"qwen3.5:0.8b","created_at":"2026-10-12T17:45:38.474338'
            '956Z","message":{"role":"assistant","content":" the"},"done"'
            ':false}\n{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:'
            '45:38.474356615Z","message":{"role":"assistant","content":" '
            'art"},"done":false}\n{"model":"qwen3.5:0.8b","created_at":"20'
            '26-10-12T17:45:38.65136126Z","message":{"role":"assistant","'
            'content":" of"},"done":false}\n{"model":"qwen3.5:0.8b","creat'
            'ed_at":"2026-10-12T17:45:38.652067881Z","message":{"role":"a'
            'ssistant","content":""},"done":true,"done_reason":"length","'
            'total_duration":4170302330,"load_duration":8948952,"prompt_e'
            'val_count":32,"prompt_eval_cached_count":28,"prompt_eval_dur'
            'ation":147900000,"eval_count":16,"eval_duration":4010030000}'
            "\n"
        ),
    },
    "n-tools": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "What's the weather like right now in São Paulo and in Zürich"
                        "? Call the tool once for each city."
                    ),
                }
            ],
            "stream": False,
            "think": False,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get the current weather for a city.",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "options": {"num_predict": 128},
        },
        "status": 200,
        "content_type": "application/json; charset=utf-8",
        ("body"): (
            '{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:46:28.49'
            '9794835Z","message":{"role":"assistant","content":"","tool_c'
            'alls":[{"id":"call_dl2l511j","function":{"index":0,"name":"g'
            'et_weather","arguments":{"city":"São Paulo"}}},{"id":"call_d'
            '6ai0jkq","function":{"index":1,"name":"get_weather","argumen'
            'ts":{"city":"Zürich"}}}]},"done":true,"done_reason":"stop","'
            'total_duration":14929683361,"load_duration":7124592,"prompt_'
            'eval_count":292,"prompt_eval_cached_count":0,"prompt_eval_du'
            'ration":6641434000,"eval_count":55,"eval_duration":825963700'
            "0}"
        ),
    },
    "n-tools-stream": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "What's the weather like right now in São Paulo and in Zürich"
                        "? Call the tool once for each city."
                    ),
                }
            ],
            "stream": True,
            "think": False,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get the current weather for a city.",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "options": {"num_predict": 128},
        },
        "status": 200,
        "content_type": "application/x-ndjson",
        ("body"): (
            '{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:46:32.59'
            '7034004Z","message":{"role":"assistant","content":"","tool_c'
            'alls":[{"id":"call_ze9h8ywf","function":{"index":0,"name":"g'
            'et_weather","arguments":{"city":"São Paulo"}}}]},"done":fals'
            'e}\n{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:46:36'
            '.577456367Z","message":{"role":"assistant","content":"","too'
            'l_calls":[{"id":"call_45te3tar","function":{"index":1,"name"'
            ':"get_weather","arguments":{"city":"Zürich"}}}]},"done":fals'
            'e}\n{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:46:36'
            '.983293203Z","message":{"role":"assistant","content":""},"do'
            'ne":true,"done_reason":"stop","total_duration":8482395999,"l'
            'oad_duration":2987698,"prompt_eval_count":292,"prompt_eval_c'
            'ached_count":288,"prompt_eval_duration":161996000,"eval_coun'
            't":55,"eval_duration":8311682000}\n'
        ),
    },
    "n-tools-cut": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "What's the weather like right now in São Paulo and in Zürich"
                        "? Call the tool once for each city."
                    ),
                }
            ],
            "stream": False,
            "think": False,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get the current weather for a city.",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "options": {"num_predict": 12},
        },
        "status": 200,
        "content_type": "application/json; charset=utf-8",
        ("body"): (
            '{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:46:38.89'
            '5308283Z","message":{"role":"assistant","content":""},"done"'
            ':true,"done_reason":"length","total_duration":1910898550,"lo'
            'ad_duration":3403813,"prompt_eval_count":292,"prompt_eval_ca'
            'ched_count":288,"prompt_eval_duration":153736000,"eval_count'
            '":12,"eval_duration":1749154000}'
        ),
    },
    "n-reasoning": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "Find the smallest positive whole number that leaves a remain"
                        "der of 1 when divided by 2, 3, 4, 5 or 6 and is divisible by"
                        " 7. Check your answer before replying."
                    ),
                }
            ],
            "stream": False,
            "think": True,
            "options": {"num_predict": 64},
        },
        "status": 200,
        "content_type": "application/json; charset=utf-8",
        ("body"): (
            '{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:45:56.51'
            '5651037Z","message":{"role":"assistant","content":"","thinki'
            'ng":"Here\'s my thought process for finding the smallest posi'
            "tive whole number that meets these conditions:\\n\\n1.  **Unde"
            "rstand the Goal:** The objective is to find an integer (let'"
            "s call it $n$) satisfying a specific set of conditions simul"
            'taneously.\\n    *   Condition A: Remainder of division by"},'
            '"done":true,"done_reason":"length","total_duration":12996144'
            '406,"load_duration":11859077,"prompt_eval_count":54,"prompt_'
            'eval_cached_count":0,"prompt_eval_duration":1234789000,"eval'
            '_count":64,"eval_duration":11707432000}'
        ),
    },
    "n-structured": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "Give the title and release year of one famous science fiction film."
                    ),
                }
            ],
            "stream": False,
            "think": False,
            "format": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "year": {"type": "integer"}},
                "required": ["title", "year"],
                "additionalProperties": False,
            },
            "options": {"num_predict": 16},
        },
        "status": 200,
        "content_type": "application/json; charset=utf-8",
        ("body"): (
            '{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:46:10.92'
            '1858122Z","message":{"role":"assistant","content":"{\\"title\\'
            '": \\"2001: A Space Odyssey\\", \\"year\\":"},"done":true,"done_'
            'reason":"length","total_duration":4170670862,"load_duration"'
            ':2775551,"prompt_eval_count":25,"prompt_eval_cached_count":0'
            ',"prompt_eval_duration":866269000,"eval_count":16,"eval_dura'
            'tion":3245992000}'
        ),
    },
    "n-structured-ok": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "Give the title and release year of one famous science fiction film."
                    ),
                }
            ],
            "stream": False,
            "think": False,
            "format": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "year": {"type": "integer"}},
                "required": ["title", "year"],
                "additionalProperties": False,
            },
            "options": {"num_predict": 200},
        },
        "status": 200,
        "content_type": "application/json; charset=utf-8",
        ("body"): (
            '{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:46:13.56'
            '9029596Z","message":{"role":"assistant","content":"{\\"title\\'
            '": \\"The Godfather\\", \\"year\\": 1972}"},"done":true,"done_re'
            'ason":"stop","total_duration":2646187086,"load_duration":277'
            '0064,"prompt_eval_count":25,"prompt_eval_cached_count":21,"p'
            'rompt_eval_duration":268253000,"eval_count":18,"eval_duratio'
            'n":2372243000}'
        ),
    },
    "n-stop": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {"role": "user", "content": "Count from 1 to 10, then write END, then count to 20."}
            ],
            "stream": False,
            "think": False,
            "options": {"num_predict": 120, "stop": ["END"]},
        },
        "status": 200,
        "content_type": "application/json; charset=utf-8",
        ("body"): (
            '{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:46:45.26'
            '9469417Z","message":{"role":"assistant","content":"Here is t'
            "he output as requested:\\n\\n```text\\n1\\n2\\n3\\n4\\n5\\n6\\n7\\n8\\n"
            '9\\n10"},"done":true,"done_reason":"stop","total_duration":63'
            '73186043,"load_duration":3249323,"prompt_eval_count":32,"pro'
            'mpt_eval_cached_count":0,"prompt_eval_duration":889959000,"e'
            'val_count":33,"eval_duration":5440095000}'
        ),
    },
    "n-unknown-model": {
        "request": {
            "model": "no-such-model:1b",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        },
        "status": 404,
        "content_type": "application/json; charset=utf-8",
        "body": '{"error":"model \'no-such-model:1b\' not found"}',
    },
    "n-bad-body": {
        "request": {"model": "qwen3.5:0.8b", "messages": "oops", "stream": False},
        "status": 400,
        "content_type": "application/json; charset=utf-8",
        ("body"): (
            '{"error":"json: cannot unmarshal string into Go struct field'
            ' .ChatRequest.messages of type []api.Message"}'
        ),
    },
    "o-text": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "Write a 200-word paragraph about how octopuses change colour"
                        " to hide from predators."
                    ),
                }
            ],
            "max_tokens": 16,
            "reasoning_effort": "none",
        },
        "status": 200,
        "content_type": "application/json",
        ("body"): (
            '{"id":"chatcmpl-553","object":"chat.completion","created":17'
            '91827248,"model":"qwen3.5:0.8b","system_fingerprint":"fp_oll'
            'ama","choices":[{"index":0,"message":{"role":"assistant","co'
            'ntent":"Octopuses exhibit a remarkable ability that rivals t'
            'he pigmentation of many other species"},"finish_reason":"len'
            'gth"}],"usage":{"prompt_tokens":32,"prompt_tokens_details":{'
            '"cached_tokens":28},"completion_tokens":16,"total_tokens":48'
            "}}\n"
        ),
    },
    "o-no-effort": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [{"role": "user", "content": "Name three moons of Jupiter."}],
            "max_tokens": 16,
        },
        "status": 200,
        "content_type": "application/json",
        ("body"): (
            '{"id":"chatcmpl-492","object":"chat.completion","created":17'
            '91827367,"model":"qwen3.5:0.8b","system_fingerprint":"fp_oll'
            'ama","choices":[{"index":0,"message":{"role":"assistant","co'
            'ntent":"","reasoning":"Thinking Process:\\n\\n1.  **Analyze th'
            'e Request:**\\n    *"},"finish_reason":"length"}],"usage":{"p'
            'rompt_tokens":16,"prompt_tokens_details":{"cached_tokens":0}'
            ',"completion_tokens":16,"total_tokens":32}}\n'
        ),
    },
    "o-text-stream-usage": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "Write a 200-word paragraph about how octopuses change colour"
                        " to hide from predators."
                    ),
                }
            ],
            "max_tokens": 16,
            "stream": True,
            "stream_options": {"include_usage": True},
            "reasoning_effort": "none",
        },
        "status": 200,
        "content_type": "text/event-stream",
        ("body"): (
            'data: {"id":"chatcmpl-782","object":"chat.completion.chunk",'
            '"created":1791827313,"model":"qwen3.5:0.8b","system_fingerpr'
            'int":"fp_ollama","choices":[{"index":0,"delta":{"role":"assi'
            'stant","content":"Oct"},"finish_reason":null}]}\n\ndata: {"id"'
            ':"chatcmpl-782","object":"chat.completion.chunk","created":1'
            '791827313,"model":"qwen3.5:0.8b","system_fingerprint":"fp_ol'
            'lama","choices":[{"index":0,"delta":{"content":"op"},"finish'
            '_reason":null}]}\n\ndata: {"id":"chatcmpl-782","object":"chat.'
            'completion.chunk","created":1791827313,"model":"qwen3.5:0.8b'
            '","system_fingerprint":"fp_ollama","choices":[{"index":0,"de'
            'lta":{"content":"uses"},"finish_reason":null}]}\n\ndata: {"id"'
            ':"chatcmpl-782","object":"chat.completion.chunk","created":1'
            '791827313,"model":"qwen3.5:0.8b","system_fingerprint":"fp_ol'
            'lama","choices":[{"index":0,"delta":{"content":" master"},"f'
            'inish_reason":null}]}\n\ndata: {"id":"chatcmpl-782","object":"'
            'chat.completion.chunk","created":1791827313,"model":"qwen3.5'
            ':0.8b","system_fingerprint":"fp_ollama","choices":[{"index":'
            '0,"delta":{"content":"fully"},"finish_reason":null}]}\n\ndata:'
            ' {"id":"chatcmpl-782","object":"chat.completion.chunk","crea'
            'ted":1791827313,"model":"qwen3.5:0.8b","system_fingerprint":'
            '"fp_ollama","choices":[{"index":0,"delta":{"content":" alter'
            '"},"finish_reason":null}]}\n\ndata: {"id":"chatcmpl-782","obje'
            'ct":"chat.completion.chunk","created":1791827313,"model":"qw'
            'en3.5:0.8b","system_fingerprint":"fp_ollama","choices":[{"in'
            'dex":0,"delta":{"content":" their"},"finish_reason":null}]}\n'
            '\ndata: {"id":"chatcmpl-782","object":"chat.completion.chunk"'
            ',"created":1791827313,"model":"qwen3.5:0.8b","system_fingerp'
            'rint":"fp_ollama","choices":[{"index":0,"delta":{"content":"'
            ' skin"},"finish_reason":null}]}\n\ndata: {"id":"chatcmpl-782",'
            '"object":"chat.completion.chunk","created":1791827313,"model'
            '":"qwen3.5:0.8b","system_fingerprint":"fp_ollama","choices":'
            '[{"index":0,"delta":{"content":" color"},"finish_reason":nul'
            'l}]}\n\ndata: {"id":"chatcmpl-782","object":"chat.completion.c'
            'hunk","created":1791827313,"model":"qwen3.5:0.8b","system_fi'
            'ngerprint":"fp_ollama","choices":[{"index":0,"delta":{"conte'
            'nt":" through"},"finish_reason":null}]}\n\ndata: {"id":"chatcm'
            'pl-782","object":"chat.completion.chunk","created":179182731'
            '3,"model":"qwen3.5:0.8b","system_fingerprint":"fp_ollama","c'
            'hoices":[{"index":0,"delta":{"content":" three"},"finish_rea'
            'son":null}]}\n\ndata: {"id":"chatcmpl-782","object":"chat.comp'
            'letion.chunk","created":1791827313,"model":"qwen3.5:0.8b","s'
            'ystem_fingerprint":"fp_ollama","choices":[{"index":0,"delta"'
            ':{"content":" distinct"},"finish_reason":null}]}\n\ndata: {"id'
            '":"chatcmpl-782","object":"chat.completion.chunk","created":'
            '1791827313,"model":"qwen3.5:0.8b","system_fingerprint":"fp_o'
            'llama","choices":[{"index":0,"delta":{"content":" colors"},"'
            'finish_reason":null}]}\n\ndata: {"id":"chatcmpl-782","object":'
            '"chat.completion.chunk","created":1791827313,"model":"qwen3.'
            '5:0.8b","system_fingerprint":"fp_ollama","choices":[{"index"'
            ':0,"delta":{"content":":"},"finish_reason":null}]}\n\ndata: {"'
            'id":"chatcmpl-782","object":"chat.completion.chunk","created'
            '":1791827313,"model":"qwen3.5:0.8b","system_fingerprint":"fp'
            '_ollama","choices":[{"index":0,"delta":{"content":" black"},'
            '"finish_reason":null}]}\n\ndata: {"id":"chatcmpl-782","object"'
            ':"chat.completion.chunk","created":1791827313,"model":"qwen3'
            '.5:0.8b","system_fingerprint":"fp_ollama","choices":[{"index'
            '":0,"delta":{"content":" represents"},"finish_reason":null}]'
            '}\n\ndata: {"id":"chatcmpl-782","object":"chat.completion.chun'
            'k","created":1791827313,"model":"qwen3.5:0.8b","system_finge'
            'rprint":"fp_ollama","choices":[{"index":0,"delta":{},"finish'
            '_reason":"length"}]}\n\ndata: {"id":"chatcmpl-782","object":"c'
            'hat.completion.chunk","created":1791827313,"model":"qwen3.5:'
            '0.8b","system_fingerprint":"fp_ollama","choices":[],"usage":'
            '{"prompt_tokens":32,"prompt_tokens_details":{"cached_tokens"'
            ':28},"completion_tokens":16,"total_tokens":48},"timings":{"p'
            'rompt_n":32,"prompt_ms":212,"prompt_per_token_ms":6.625,"pro'
            'mpt_per_second":150.9433962264151,"predicted_n":16,"predicte'
            'd_ms":4558,"predicted_per_token_ms":284.875,"predicted_per_s'
            'econd":3.5103115401491882}}\n\ndata: [DONE]\n\n'
        ),
    },
    "o-text-stream": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "Write a 200-word paragraph about how octopuses change colour"
                        " to hide from predators."
                    ),
                }
            ],
            "max_tokens": 16,
            "stream": True,
            "reasoning_effort": "none",
        },
        "status": 200,
        "content_type": "text/event-stream",
        ("body"): (
            'data: {"id":"chatcmpl-236","object":"chat.completion.chunk",'
            '"created":1791827318,"model":"qwen3.5:0.8b","system_fingerpr'
            'int":"fp_ollama","choices":[{"index":0,"delta":{"role":"assi'
            'stant","content":"Deep"},"finish_reason":null}]}\n\ndata: {"id'
            '":"chatcmpl-236","object":"chat.completion.chunk","created":'
            '1791827318,"model":"qwen3.5:0.8b","system_fingerprint":"fp_o'
            'llama","choices":[{"index":0,"delta":{"content":"-se"},"fini'
            'sh_reason":null}]}\n\ndata: {"id":"chatcmpl-236","object":"cha'
            't.completion.chunk","created":1791827318,"model":"qwen3.5:0.'
            '8b","system_fingerprint":"fp_ollama","choices":[{"index":0,"'
            'delta":{"content":"a"},"finish_reason":null}]}\n\ndata: {"id":'
            '"chatcmpl-236","object":"chat.completion.chunk","created":17'
            '91827318,"model":"qwen3.5:0.8b","system_fingerprint":"fp_oll'
            'ama","choices":[{"index":0,"delta":{"content":" oct"},"finis'
            'h_reason":null}]}\n\ndata: {"id":"chatcmpl-236","object":"chat'
            '.completion.chunk","created":1791827318,"model":"qwen3.5:0.8'
            'b","system_fingerprint":"fp_ollama","choices":[{"index":0,"d'
            'elta":{"content":"op"},"finish_reason":null}]}\n\ndata: {"id":'
            '"chatcmpl-236","object":"chat.completion.chunk","created":17'
            '91827318,"model":"qwen3.5:0.8b","system_fingerprint":"fp_oll'
            'ama","choices":[{"index":0,"delta":{"content":"uses"},"finis'
            'h_reason":null}]}\n\ndata: {"id":"chatcmpl-236","object":"chat'
            '.completion.chunk","created":1791827318,"model":"qwen3.5:0.8'
            'b","system_fingerprint":"fp_ollama","choices":[{"index":0,"d'
            'elta":{"content":" possess"},"finish_reason":null}]}\n\ndata: '
            '{"id":"chatcmpl-236","object":"chat.completion.chunk","creat'
            'ed":1791827318,"model":"qwen3.5:0.8b","system_fingerprint":"'
            'fp_ollama","choices":[{"index":0,"delta":{"content":" an"},"'
            'finish_reason":null}]}\n\ndata: {"id":"chatcmpl-236","object":'
            '"chat.completion.chunk","created":1791827318,"model":"qwen3.'
            '5:0.8b","system_fingerprint":"fp_ollama","choices":[{"index"'
            ':0,"delta":{"content":" incredibly"},"finish_reason":null}]}'
            '\n\ndata: {"id":"chatcmpl-236","object":"chat.completion.chunk'
            '","created":1791827318,"model":"qwen3.5:0.8b","system_finger'
            'print":"fp_ollama","choices":[{"index":0,"delta":{"content":'
            '" vibrant"},"finish_reason":null}]}\n\ndata: {"id":"chatcmpl-2'
            '36","object":"chat.completion.chunk","created":1791827318,"m'
            'odel":"qwen3.5:0.8b","system_fingerprint":"fp_ollama","choic'
            'es":[{"index":0,"delta":{"content":" spectrum"},"finish_reas'
            'on":null}]}\n\ndata: {"id":"chatcmpl-236","object":"chat.compl'
            'etion.chunk","created":1791827318,"model":"qwen3.5:0.8b","sy'
            'stem_fingerprint":"fp_ollama","choices":[{"index":0,"delta":'
            '{"content":" of"},"finish_reason":null}]}\n\ndata: {"id":"chat'
            'cmpl-236","object":"chat.completion.chunk","created":1791827'
            '318,"model":"qwen3.5:0.8b","system_fingerprint":"fp_ollama",'
            '"choices":[{"index":0,"delta":{"content":" color"},"finish_r'
            'eason":null}]}\n\ndata: {"id":"chatcmpl-236","object":"chat.co'
            'mpletion.chunk","created":1791827318,"model":"qwen3.5:0.8b",'
            '"system_fingerprint":"fp_ollama","choices":[{"index":0,"delt'
            'a":{"content":"ation"},"finish_reason":null}]}\n\ndata: {"id":'
            '"chatcmpl-236","object":"chat.completion.chunk","created":17'
            '91827318,"model":"qwen3.5:0.8b","system_fingerprint":"fp_oll'
            'ama","choices":[{"index":0,"delta":{"content":" ranging"},"f'
            'inish_reason":null}]}\n\ndata: {"id":"chatcmpl-236","object":"'
            'chat.completion.chunk","created":1791827318,"model":"qwen3.5'
            ':0.8b","system_fingerprint":"fp_ollama","choices":[{"index":'
            '0,"delta":{"content":" from"},"finish_reason":null}]}\n\ndata:'
            ' {"id":"chatcmpl-236","object":"chat.completion.chunk","crea'
            'ted":1791827318,"model":"qwen3.5:0.8b","system_fingerprint":'
            '"fp_ollama","choices":[{"index":0,"delta":{},"finish_reason"'
            ':"length"}]}\n\ndata: [DONE]\n\n'
        ),
    },
    "o-tools": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "What's the weather like right now in São Paulo and in Zürich"
                        "? Call the tool once for each city."
                    ),
                }
            ],
            "max_tokens": 128,
            "reasoning_effort": "none",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get the current weather for a city.",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "tool_choice": "auto",
        },
        "status": 200,
        "content_type": "application/json",
        ("body"): (
            '{"id":"chatcmpl-32","object":"chat.completion","created":179'
            '1827345,"model":"qwen3.5:0.8b","system_fingerprint":"fp_olla'
            'ma","choices":[{"index":0,"message":{"role":"assistant","con'
            'tent":"","tool_calls":[{"id":"call_a4lqvtzn","index":0,"type'
            '":"function","function":{"name":"get_weather","arguments":"{'
            '\\"city\\":\\"São Paulo\\"}"}},{"id":"call_dhrtg4n2","index":1,"'
            'type":"function","function":{"name":"get_weather","arguments'
            '":"{\\"city\\":\\"Zürich\\"}"}}]},"finish_reason":"tool_calls"}]'
            ',"usage":{"prompt_tokens":292,"prompt_tokens_details":{"cach'
            'ed_tokens":288},"completion_tokens":55,"total_tokens":347}}\n'
        ),
    },
    "o-tools-stream": {
        "request": {
            "model": "qwen3.5:0.8b",
            "messages": [
                {
                    "role": "user",
                    ("content"): (
                        "What's the weather like right now in São Paulo and in Zürich"
                        "? Call the tool once for each city."
                    ),
                }
            ],
            "max_tokens": 128,
            "stream": True,
            "reasoning_effort": "none",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get the current weather for a city.",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "tool_choice": "auto",
        },
        "status": 200,
        "content_type": "text/event-stream",
        ("body"): (
            'data: {"id":"chatcmpl-378","object":"chat.completion.chunk",'
            '"created":1791827349,"model":"qwen3.5:0.8b","system_fingerpr'
            'int":"fp_ollama","choices":[{"index":0,"delta":{"role":"assi'
            'stant","content":"","tool_calls":[{"id":"call_pya066e5","ind'
            'ex":0,"type":"function","function":{"name":"get_weather","ar'
            'guments":"{\\"city\\":\\"São Paulo\\"}"}}]},"finish_reason":null'
            '}]}\n\ndata: {"id":"chatcmpl-378","object":"chat.completion.ch'
            'unk","created":1791827349,"model":"qwen3.5:0.8b","system_fin'
            'gerprint":"fp_ollama","choices":[{"index":0,"delta":{"conten'
            't":"","tool_calls":[{"id":"call_9b8oxvh3","index":1,"type":"'
            'function","function":{"name":"get_weather","arguments":"{\\"c'
            'ity\\":\\"Zürich\\"}"}}]},"finish_reason":null}]}\n\ndata: {"id":'
            '"chatcmpl-378","object":"chat.completion.chunk","created":17'
            '91827349,"model":"qwen3.5:0.8b","system_fingerprint":"fp_oll'
            'ama","choices":[{"index":0,"delta":{},"finish_reason":"tool_'
            'calls"}]}\n\ndata: [DONE]\n\n'
        ),
    },
    "o-unknown-model": {
        "request": {"model": "no-such-model:1b", "messages": [{"role": "user", "content": "hi"}]},
        "status": 404,
        "content_type": "application/json",
        ("body"): (
            '{"error":{"message":"model \'no-such-model:1b\' not found","ty'
            'pe":"not_found_error","param":null,"code":null}}\n'
        ),
    },
}


def exchange(name="n-text", *, end="clean", kind=None):
    probe = PROBES[name]
    body, _ = response_body(
        Response(
            probe["status"], {"content-type": probe["content_type"]}, [probe["body"].encode()], end
        ),
        False,
    )
    body_kind, data = next(iter(body.items()))
    if kind == "chunks_b64":
        body_kind, data = kind, [base64.b64encode(probe["body"].encode()).decode()]
    elif kind == "text":
        body_kind, data = kind, probe["body"]
    parts = body_kind in ("ndjson", "sse", "chunks_b64")
    return Exchange(
        "POST",
        "/api/chat" if name.startswith("n-") else "/v1/chat/completions",
        probe["status"],
        {"content-type": probe["content_type"]},
        Body(body_kind, None if parts else data, tuple(data) if parts else ()),
        end,
    )


# First two lines and the final line of n-reasoning-stream.
NATIVE_REASONING_LINES = (
    (
        '{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:45:57.25'
        '9851959Z","message":{"role":"assistant","content":"","thinki'
        'ng":"Thinking"},"done":false}'
    ),
    (
        '{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:45:57.70'
        '3065567Z","message":{"role":"assistant","content":"","thinki'
        'ng":" Process"},"done":false}'
    ),
    (
        '{"model":"qwen3.5:0.8b","created_at":"2026-10-12T17:46:06.75'
        '0272446Z","message":{"role":"assistant","content":""},"done"'
        ':true,"done_reason":"length","total_duration":10233588196,"l'
        'oad_duration":3233225,"prompt_eval_count":23,"prompt_eval_ca'
        'ched_count":0,"prompt_eval_duration":704118000,"eval_count":'
        '48,"eval_duration":9486120000}'
    ),
)
