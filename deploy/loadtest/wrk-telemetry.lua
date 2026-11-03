wrk.method = "POST"
wrk.headers["Content-Type"] = "application/json"
wrk.headers["X-Mesh-Gateway-Key"] = "bench"
counter = math.random(1, 100000000)
request = function()
  counter = counter + 1
  local body = string.format('{"gatewayId":"gw-%d","observations":[{"node":"N%d","seq":%d,"up":%d,"mq2":120.5,"tempC":31.2,"vibG":0.02,"mic":40}]}', counter % 50, counter % 500, counter, counter)
  return wrk.format(nil, "/api/v1/ingest/telemetry", nil, body)
end
