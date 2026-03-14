package main

import (
	"bytes"
	"compress/gzip"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

type response struct {
	Success bool   `json:"success"`
	Message string `json:"message"`
}

type orderRequest struct {
	Exchange          string  `json:"exchange"`
	Symbol            string  `json:"symbol"`
	Side              string  `json:"side"`
	QuantityUSD       float64 `json:"quantity_usd"`
	QuantityContracts float64 `json:"quantity_contracts"`
	QuantityBase      float64 `json:"quantity_base"`
	OrderType         string  `json:"order_type"`
	LimitPrice        float64 `json:"limit_price"`
	Offset            string  `json:"offset"`
}

type orderStatusRequest struct {
	Exchange string `json:"exchange"`
	Symbol   string `json:"symbol"`
	OrderID  string `json:"order_id"`
}

type bookSnapshot struct {
	Bid float64 `json:"bid"`
	Ask float64 `json:"ask"`
	Ts  int64   `json:"ts"`
}

type instrumentCache struct {
	mu        sync.RWMutex
	tickSize  map[string]map[string]float64
	minQty    map[string]map[string]float64
}

func newInstrumentCache() *instrumentCache {
	return &instrumentCache{
		tickSize: map[string]map[string]float64{},
		minQty:   map[string]map[string]float64{},
	}
}

func (c *instrumentCache) set(exchange, symbol string, tick, minQty float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.tickSize[exchange] == nil {
		c.tickSize[exchange] = map[string]float64{}
	}
	if c.minQty[exchange] == nil {
		c.minQty[exchange] = map[string]float64{}
	}
	if tick > 0 {
		c.tickSize[exchange][symbol] = tick
	}
	if minQty > 0 {
		c.minQty[exchange][symbol] = minQty
	}
}

func (c *instrumentCache) round(exchange, symbol string, qty, price float64) (float64, float64) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if tick, ok := c.tickSize[exchange][symbol]; ok && tick > 0 && price > 0 {
		price = float64(int(price/tick)) * tick
	}
	if minq, ok := c.minQty[exchange][symbol]; ok && minq > 0 && qty > 0 {
		step := minq
		qty = float64(int(qty/step)) * step
	}
	return qty, price
}

type metricStore struct {
	mu     sync.Mutex
	count  map[string]int64
	errors map[string]int64
	latSum map[string]float64
	latCnt map[string]int64
}

func newMetricStore() *metricStore {
	return &metricStore{
		count:  map[string]int64{},
		errors: map[string]int64{},
		latSum: map[string]float64{},
		latCnt: map[string]int64{},
	}
}

func (m *metricStore) observe(name string, ms float64, err bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.count[name]++
	if err {
		m.errors[name]++
	}
	m.latSum[name] += ms
	m.latCnt[name]++
}

type rateLimiter struct {
	ch chan struct{}
}

func newRateLimiter(rps int) *rateLimiter {
	if rps <= 0 {
		rps = 5
	}
	rl := &rateLimiter{ch: make(chan struct{}, rps)}
	t := time.NewTicker(time.Second / time.Duration(rps))
	go func() {
		for range t.C {
			select {
			case rl.ch <- struct{}{}:
			default:
			}
		}
	}()
	return rl
}

type okxClient struct {
	apiKey     string
	apiSecret  string
	passphrase string
	baseURL    string
	rlOrder    *rateLimiter
	rlQuery    *rateLimiter
	metrics    *metricStore
}

func newOKXClient() *okxClient {
	base := "https://www.okx.com"
	if v := strings.TrimSpace(os.Getenv("OKX_BASE_URL")); v != "" {
		base = v
	}
	if strings.ToLower(os.Getenv("OKX_TESTNET")) == "true" {
		base = "https://www.okx.com"
	}
	secret := os.Getenv("OKX_API_SECRET")
	if secret == "" {
		secret = os.Getenv("OKX_SECRET")
	}
	return &okxClient{
		apiKey:     os.Getenv("OKX_API_KEY"),
		apiSecret:  secret,
		passphrase: os.Getenv("OKX_PASSPHRASE"),
		baseURL:    base,
		rlOrder:    newRateLimiter(envInt("OKX_RPS_ORDER", 5)),
		rlQuery:    newRateLimiter(envInt("OKX_RPS_QUERY", 8)),
		metrics:    newMetricStore(),
	}
}

func (c *okxClient) sign(ts, method, requestPath, body string) string {
	prehash := ts + method + requestPath + body
	mac := hmac.New(sha256.New, []byte(c.apiSecret))
	mac.Write([]byte(prehash))
	return base64.StdEncoding.EncodeToString(mac.Sum(nil))
}

func (c *okxClient) request(method, path string, params url.Values, body []byte) (map[string]any, error) {
	start := time.Now()
	if strings.Contains(path, "/trade/order") {
		<-c.rlOrder.ch
	} else {
		<-c.rlQuery.ch
	}
	reqPath := path
	if params != nil && len(params) > 0 {
		reqPath = reqPath + "?" + params.Encode()
	}
	ts := time.Now().UTC().Format("2006-01-02T15:04:05.000Z")
	sign := c.sign(ts, method, reqPath, string(body))
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		req, err := http.NewRequest(method, c.baseURL+reqPath, bytes.NewBuffer(body))
		if err != nil {
			lastErr = err
			continue
		}
		req.Header.Set("OK-ACCESS-KEY", c.apiKey)
		req.Header.Set("OK-ACCESS-SIGN", sign)
		req.Header.Set("OK-ACCESS-TIMESTAMP", ts)
		req.Header.Set("OK-ACCESS-PASSPHRASE", c.passphrase)
		req.Header.Set("Content-Type", "application/json")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			lastErr = err
			time.Sleep(time.Duration(100*(attempt+1)) * time.Millisecond)
			continue
		}
		defer resp.Body.Close()
		var out map[string]any
		if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
			lastErr = err
			time.Sleep(time.Duration(100*(attempt+1)) * time.Millisecond)
			continue
		}
		c.metrics.observe("okx_"+path, float64(time.Since(start).Milliseconds()), resp.StatusCode >= 400)
		return out, nil
	}
	c.metrics.observe("okx_"+path, float64(time.Since(start).Milliseconds()), true)
	return nil, lastErr
}

func (c *okxClient) placeOrder(symbol, side, orderType string, qty float64, price float64, spot bool) (map[string]any, error) {
	if c.apiKey == "" || c.apiSecret == "" {
		return nil, errors.New("missing okx keys")
	}
	inst := strings.ReplaceAll(symbol, "USDT", "-USDT")
	if !strings.HasSuffix(inst, "-SWAP") && !spot {
		inst = inst + "-SWAP"
	}
	data := map[string]any{
		"instId": inst,
		"side":   side,
		"ordType": func() string {
			if orderType == "ioc" && price > 0 {
				return "ioc"
			}
			if orderType == "limit" {
				return "limit"
			}
			return "market"
		}(),
		"sz": fmt.Sprintf("%f", qty),
	}
	if spot {
		data["tdMode"] = "cash"
	} else {
		data["tdMode"] = "cross"
	}
	if price > 0 {
		data["px"] = fmt.Sprintf("%f", price)
	}
	body, _ := json.Marshal(data)
	return c.request("POST", "/api/v5/trade/order", nil, body)
}

func (c *okxClient) orderStatus(symbol, orderID string, spot bool) (map[string]any, error) {
	if c.apiKey == "" || c.apiSecret == "" {
		return nil, errors.New("missing okx keys")
	}
	inst := strings.ReplaceAll(symbol, "USDT", "-USDT")
	if !strings.HasSuffix(inst, "-SWAP") && !spot {
		inst = inst + "-SWAP"
	}
	params := url.Values{}
	params.Set("instId", inst)
	params.Set("ordId", orderID)
	return c.request("GET", "/api/v5/trade/order", params, nil)
}

func (c *okxClient) balances() (map[string]any, error) {
	if c.apiKey == "" || c.apiSecret == "" {
		return nil, errors.New("missing okx keys")
	}
	return c.request("GET", "/api/v5/account/balance", nil, nil)
}

type bybitClient struct {
	apiKey    string
	apiSecret string
	baseURL   string
	rlOrder   *rateLimiter
	rlQuery   *rateLimiter
	metrics   *metricStore
}

type htxClient struct {
	apiKey    string
	apiSecret string
	baseURL   string
	spotBaseURL string
	rlOrder   *rateLimiter
	rlQuery   *rateLimiter
	metrics   *metricStore
	mu        sync.Mutex
	spotAccountID string
}

func newBybitClient() *bybitClient {
	base := "https://api.bybit.com"
	if v := strings.TrimSpace(os.Getenv("BYBIT_BASE_URL")); v != "" {
		base = v
	}
	if strings.ToLower(os.Getenv("BYBIT_TESTNET")) == "true" {
		base = "https://api-testnet.bybit.com"
	}
	secret := os.Getenv("BYBIT_API_SECRET")
	if secret == "" {
		secret = os.Getenv("BYBIT_SECRET")
	}
	return &bybitClient{
		apiKey:    os.Getenv("BYBIT_API_KEY"),
		apiSecret: secret,
		baseURL:   base,
		rlOrder:   newRateLimiter(envInt("BYBIT_RPS_ORDER", 5)),
		rlQuery:   newRateLimiter(envInt("BYBIT_RPS_QUERY", 8)),
		metrics:   newMetricStore(),
	}
}

func newHTXClient() *htxClient {
	base := "https://api.hbdm.com"
	if v := strings.TrimSpace(os.Getenv("HTX_BASE_URL")); v != "" {
		base = v
	}
	spotBase := "https://api.htx.com"
	if v := strings.TrimSpace(os.Getenv("HTX_SPOT_BASE_URL")); v != "" {
		spotBase = v
	}
	secret := os.Getenv("HTX_API_SECRET")
	if secret == "" {
		secret = os.Getenv("HTX_SECRET")
	}
	return &htxClient{
		apiKey:    os.Getenv("HTX_API_KEY"),
		apiSecret: secret,
		baseURL:   base,
		spotBaseURL: spotBase,
		rlOrder:   newRateLimiter(envInt("HTX_RPS_ORDER", 5)),
		rlQuery:   newRateLimiter(envInt("HTX_RPS_QUERY", 8)),
		metrics:   newMetricStore(),
	}
}

func (c *bybitClient) sign(ts, recvWindow, payload string) string {
	mac := hmac.New(sha256.New, []byte(c.apiSecret))
	mac.Write([]byte(ts + c.apiKey + recvWindow + payload))
	return fmt.Sprintf("%x", mac.Sum(nil))
}

func (c *bybitClient) request(method, path string, params url.Values, body []byte) (map[string]any, error) {
	start := time.Now()
	if strings.Contains(path, "/order") {
		<-c.rlOrder.ch
	} else {
		<-c.rlQuery.ch
	}
	recv := "5000"
	ts := strconv.FormatInt(time.Now().UnixMilli(), 10)
	query := ""
	if params != nil && len(params) > 0 {
		query = params.Encode()
	}
	payload := query
	if method == "POST" {
		payload = string(body)
	}
	sign := c.sign(ts, recv, payload)
	reqURL := c.baseURL + path
	if query != "" {
		reqURL = reqURL + "?" + query
	}
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		req, err := http.NewRequest(method, reqURL, bytes.NewBuffer(body))
		if err != nil {
			lastErr = err
			continue
		}
		req.Header.Set("X-BAPI-API-KEY", c.apiKey)
		req.Header.Set("X-BAPI-TIMESTAMP", ts)
		req.Header.Set("X-BAPI-SIGN", sign)
		req.Header.Set("X-BAPI-RECV-WINDOW", recv)
		req.Header.Set("Content-Type", "application/json")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			lastErr = err
			time.Sleep(time.Duration(100*(attempt+1)) * time.Millisecond)
			continue
		}
		defer resp.Body.Close()
		var out map[string]any
		if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
			lastErr = err
			time.Sleep(time.Duration(100*(attempt+1)) * time.Millisecond)
			continue
		}
		c.metrics.observe("bybit_"+path, float64(time.Since(start).Milliseconds()), resp.StatusCode >= 400)
		return out, nil
	}
	c.metrics.observe("bybit_"+path, float64(time.Since(start).Milliseconds()), true)
	return nil, lastErr
}

func (c *bybitClient) placeOrder(symbol, side, orderType string, qty float64, price float64, spot bool) (map[string]any, error) {
	if c.apiKey == "" || c.apiSecret == "" {
		return nil, errors.New("missing bybit keys")
	}
	category := "linear"
	if spot {
		category = "spot"
	}
	body := map[string]any{
		"category":  category,
		"symbol":    symbol,
		"side":      strings.Title(side),
		"orderType": strings.Title(orderType),
		"qty":       fmt.Sprintf("%f", qty),
	}
	if price > 0 {
		body["price"] = fmt.Sprintf("%f", price)
	}
	payload, _ := json.Marshal(body)
	return c.request("POST", "/v5/order/create", nil, payload)
}

func (c *bybitClient) orderStatus(symbol, orderID string, spot bool) (map[string]any, error) {
	if c.apiKey == "" || c.apiSecret == "" {
		return nil, errors.New("missing bybit keys")
	}
	category := "linear"
	if spot {
		category = "spot"
	}
	params := url.Values{}
	params.Set("category", category)
	params.Set("symbol", symbol)
	params.Set("orderId", orderID)
	return c.request("GET", "/v5/order/realtime", params, nil)
}

func (c *bybitClient) balances() (map[string]any, error) {
	if c.apiKey == "" || c.apiSecret == "" {
		return nil, errors.New("missing bybit keys")
	}
	params := url.Values{}
	params.Set("accountType", "UNIFIED")
	return c.request("GET", "/v5/account/wallet-balance", params, nil)
}

func (c *htxClient) sign(method, host, path string, params url.Values) url.Values {
	if params == nil {
		params = url.Values{}
	}
	params.Set("AccessKeyId", c.apiKey)
	params.Set("SignatureMethod", "HmacSHA256")
	params.Set("SignatureVersion", "2")
	params.Set("Timestamp", time.Now().UTC().Format("2006-01-02T15:04:05"))
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var sb strings.Builder
	for i, k := range keys {
		if i > 0 {
			sb.WriteString("&")
		}
		sb.WriteString(url.QueryEscape(k))
		sb.WriteString("=")
		sb.WriteString(url.QueryEscape(params.Get(k)))
	}
	canonical := strings.Join([]string{strings.ToUpper(method), host, path, sb.String()}, "\n")
	mac := hmac.New(sha256.New, []byte(c.apiSecret))
	mac.Write([]byte(canonical))
	signature := base64.StdEncoding.EncodeToString(mac.Sum(nil))
	params.Set("Signature", signature)
	return params
}

func (c *htxClient) request(method, path string, params url.Values, body []byte) (map[string]any, error) {
	return c.requestBase(c.baseURL, method, path, params, body)
}

func (c *htxClient) requestBase(baseURL, method, path string, params url.Values, body []byte) (map[string]any, error) {
	start := time.Now()
	if strings.Contains(path, "/swap_cross_order") || strings.Contains(path, "/swap_order") {
		<-c.rlOrder.ch
	} else {
		<-c.rlQuery.ch
	}
	if c.apiKey == "" || c.apiSecret == "" {
		return nil, errors.New("missing htx keys")
	}
	host := strings.TrimPrefix(strings.TrimPrefix(baseURL, "https://"), "http://")
	signed := c.sign(method, host, path, params)
	reqURL := baseURL + path
	if len(signed) > 0 {
		reqURL = reqURL + "?" + signed.Encode()
	}
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		req, err := http.NewRequest(method, reqURL, bytes.NewBuffer(body))
		if err != nil {
			lastErr = err
			continue
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			lastErr = err
			time.Sleep(time.Duration(100*(attempt+1)) * time.Millisecond)
			continue
		}
		defer resp.Body.Close()
		var out map[string]any
		if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
			lastErr = err
			time.Sleep(time.Duration(100*(attempt+1)) * time.Millisecond)
			continue
		}
		c.metrics.observe("htx_"+path, float64(time.Since(start).Milliseconds()), resp.StatusCode >= 400)
		return out, nil
	}
	c.metrics.observe("htx_"+path, float64(time.Since(start).Milliseconds()), true)
	return nil, lastErr
}

func (c *htxClient) placeOrder(symbol, side, orderType string, qty float64, price float64, offset string) (map[string]any, error) {
	if c.apiKey == "" || c.apiSecret == "" {
		return nil, errors.New("missing htx keys")
	}
	contract := usdtToHTX(symbol)
	htxType := strings.ToLower(orderType)
	if htxType == "market" {
		htxType = "opponent"
	}
	body := map[string]any{
		"contract_code":  contract,
		"direction":      strings.ToLower(side),
		"offset":         offset,
		"lever_rate":     1,
		"order_price_type": htxType,
		"volume":         qty,
		"margin_account": "USDT",
	}
	if htxType == "limit" && price > 0 {
		body["price"] = price
	}
	payload, _ := json.Marshal(body)
	return c.request("POST", "/linear-swap-api/v1/swap_cross_order", nil, payload)
}

func (c *htxClient) orderStatus(symbol, orderID string) (map[string]any, error) {
	contract := usdtToHTX(symbol)
	body := map[string]any{
		"contract_code": contract,
		"order_id":      orderID,
	}
	payload, _ := json.Marshal(body)
	return c.request("POST", "/linear-swap-api/v1/swap_order_info", nil, payload)
}

func (c *htxClient) balances() (map[string]any, error) {
	return c.request("POST", "/linear-swap-api/v3/unified_account_info", nil, []byte("{}"))
}

func (c *htxClient) getSpotAccountID() (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.spotAccountID != "" {
		return c.spotAccountID, nil
	}
	resp, err := c.requestBase(c.spotBaseURL, "GET", "/v1/account/accounts", nil, nil)
	if err != nil {
		return "", err
	}
	data, ok := resp["data"].([]any)
	if !ok {
		return "", errors.New("htx_spot_accounts_unavailable")
	}
	for _, row := range data {
		item, ok := row.(map[string]any)
		if !ok {
			continue
		}
		if fmt.Sprintf("%v", item["type"]) == "spot" && fmt.Sprintf("%v", item["state"]) == "working" {
			c.spotAccountID = fmt.Sprintf("%v", item["id"])
			break
		}
	}
	if c.spotAccountID == "" {
		return "", errors.New("htx_spot_account_missing")
	}
	return c.spotAccountID, nil
}

func (c *htxClient) placeSpotOrder(symbol, side, orderType string, qty float64, price float64) (map[string]any, error) {
	if c.apiKey == "" || c.apiSecret == "" {
		return nil, errors.New("missing htx keys")
	}
	accountID, err := c.getSpotAccountID()
	if err != nil {
		return nil, err
	}
	sym := strings.ToLower(strings.ReplaceAll(symbol, "-", ""))
	orderTypeLower := strings.ToLower(orderType)
	otype := "buy-limit"
	if orderTypeLower == "market" || orderTypeLower == "opponent" {
		if strings.ToLower(side) == "buy" {
			otype = "buy-market"
		} else {
			otype = "sell-market"
		}
	} else {
		if strings.ToLower(side) == "buy" {
			otype = "buy-limit"
		} else {
			otype = "sell-limit"
		}
	}
	body := map[string]any{
		"account-id": accountID,
		"symbol":     sym,
		"type":       otype,
		"amount":     fmt.Sprintf("%f", qty),
	}
	if strings.HasSuffix(otype, "limit") && price > 0 {
		body["price"] = fmt.Sprintf("%f", price)
	}
	payload, _ := json.Marshal(body)
	return c.requestBase(c.spotBaseURL, "POST", "/v1/order/orders/place", nil, payload)
}

func (c *htxClient) spotOrderStatus(orderID string) (map[string]any, error) {
	path := fmt.Sprintf("/v1/order/orders/%s", orderID)
	return c.requestBase(c.spotBaseURL, "GET", path, nil, nil)
}

type bookCache struct {
	mu    sync.RWMutex
	data  map[string]map[string]bookSnapshot
}

func newBookCache() *bookCache {
	return &bookCache{data: map[string]map[string]bookSnapshot{}}
}

func (c *bookCache) set(exchange, symbol string, bid, ask float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.data[exchange] == nil {
		c.data[exchange] = map[string]bookSnapshot{}
	}
	c.data[exchange][symbol] = bookSnapshot{Bid: bid, Ask: ask, Ts: time.Now().UnixMilli()}
}

func (c *bookCache) get(exchange, symbol string) (bookSnapshot, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	b, ok := c.data[exchange][symbol]
	return b, ok
}

func main() {
	addr := ":8089"
	if v := os.Getenv("LOWLATENCY_ADDR"); v != "" {
		addr = v
	}

	exchanges := enabledExchanges()
	okx := newOKXClient()
	var bybit *bybitClient
	if exchanges["bybit"] {
		bybit = newBybitClient()
	}
	var htx *htxClient
	if exchanges["htx"] {
		htx = newHTXClient()
	}
	books := newBookCache()
	inst := newInstrumentCache()
	if exchanges["okx"] {
		go loadOKXInstruments(inst)
	}
	if exchanges["bybit"] {
		go loadBybitInstruments(inst)
	}
	if exchanges["htx"] {
		go loadHTXInstruments(inst)
	}

	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
	})

	http.HandleFunc("/execute", func(w http.ResponseWriter, r *http.Request) {
		var req orderRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		qty := req.QuantityContracts
		if qty <= 0 {
			qty = req.QuantityUSD
		}
		var out map[string]any
		var err error
		ex := strings.ToLower(req.Exchange)
		if ex == "" || ex == "auto" {
			ex = routeExchange(books, req.Side, req.Symbol)
		}
		qty, px := inst.round(ex, req.Symbol, qty, req.LimitPrice)
		switch ex {
		case "okx":
			out, err = okx.placeOrder(req.Symbol, req.Side, req.OrderType, qty, px, false)
		case "bybit":
			if bybit == nil {
				err = errors.New("bybit_disabled")
				break
			}
			out, err = bybit.placeOrder(req.Symbol, req.Side, req.OrderType, qty, px, false)
		case "htx":
			if htx == nil {
				err = errors.New("htx_disabled")
				break
			}
			offset := req.Offset
			if offset == "" {
				offset = "open"
			}
			out, err = htx.placeOrder(req.Symbol, req.Side, req.OrderType, qty, px, offset)
		default:
			if ex == "okx" {
				out, err = okx.placeOrder(req.Symbol, req.Side, req.OrderType, qty, req.LimitPrice, false)
			} else if ex == "bybit" {
				if bybit == nil {
					err = errors.New("bybit_disabled")
				} else {
					out, err = bybit.placeOrder(req.Symbol, req.Side, req.OrderType, qty, req.LimitPrice, false)
				}
			} else if ex == "htx" {
				if htx == nil {
					err = errors.New("htx_disabled")
				} else {
					offset := req.Offset
					if offset == "" {
						offset = "open"
					}
					out, err = htx.placeOrder(req.Symbol, req.Side, req.OrderType, qty, req.LimitPrice, offset)
				}
			} else {
				err = errors.New("unsupported exchange")
			}
		}
		if err != nil {
			_ = json.NewEncoder(w).Encode(response{Success: false, Message: err.Error()})
			return
		}
		resp := normalizeResponse(ex, out)
		resp["size"] = qty
		if snap, ok := books.get(ex, req.Symbol); ok {
			resp["fill_price"] = (snap.Bid + snap.Ask) / 2
		}
		_ = json.NewEncoder(w).Encode(resp)
	})
	http.HandleFunc("/spot_execute", func(w http.ResponseWriter, r *http.Request) {
		var req orderRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		qty := req.QuantityBase
		if qty <= 0 && req.QuantityUSD > 0 {
			if snap, ok := books.get(strings.ToLower(req.Exchange), req.Symbol); ok {
				if strings.ToLower(req.Side) == "buy" && snap.Ask > 0 {
					qty = req.QuantityUSD / snap.Ask
				}
				if strings.ToLower(req.Side) == "sell" && snap.Bid > 0 {
					qty = req.QuantityUSD / snap.Bid
				}
			}
		}
		var out map[string]any
		var err error
		ex := strings.ToLower(req.Exchange)
		if ex == "" || ex == "auto" {
			ex = routeExchange(books, req.Side, req.Symbol)
		}
		qty, px := inst.round(ex, req.Symbol, qty, req.LimitPrice)
		switch ex {
		case "okx":
			out, err = okx.placeOrder(req.Symbol, req.Side, req.OrderType, qty, px, true)
		case "bybit":
			if bybit == nil {
				err = errors.New("bybit_disabled")
				break
			}
			out, err = bybit.placeOrder(req.Symbol, req.Side, req.OrderType, qty, px, true)
		case "htx":
			if htx == nil {
				err = errors.New("htx_disabled")
				break
			}
			out, err = htx.placeSpotOrder(req.Symbol, req.Side, req.OrderType, qty, px)
		default:
			err = errors.New("unsupported exchange")
		}
		if err != nil {
			_ = json.NewEncoder(w).Encode(response{Success: false, Message: err.Error()})
			return
		}
		resp := normalizeResponse(ex, out)
		resp["size"] = qty
		if snap, ok := books.get(ex, req.Symbol); ok {
			resp["fill_price"] = (snap.Bid + snap.Ask) / 2
		}
		_ = json.NewEncoder(w).Encode(resp)
	})
	http.HandleFunc("/order", func(w http.ResponseWriter, r *http.Request) {
		var req orderStatusRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		var out map[string]any
		var err error
		switch strings.ToLower(req.Exchange) {
		case "okx":
			out, err = okx.orderStatus(req.Symbol, req.OrderID, false)
		case "bybit":
			if bybit == nil {
				err = errors.New("bybit_disabled")
				break
			}
			out, err = bybit.orderStatus(req.Symbol, req.OrderID, false)
		case "htx":
			if htx == nil {
				err = errors.New("htx_disabled")
				break
			}
			out, err = htx.orderStatus(req.Symbol, req.OrderID)
		default:
			err = errors.New("unsupported exchange")
		}
		if err != nil {
			_ = json.NewEncoder(w).Encode(response{Success: false, Message: err.Error()})
			return
		}
		_ = json.NewEncoder(w).Encode(out)
	})
	http.HandleFunc("/spot_order", func(w http.ResponseWriter, r *http.Request) {
		var req orderStatusRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		var out map[string]any
		var err error
		switch strings.ToLower(req.Exchange) {
		case "okx":
			out, err = okx.orderStatus(req.Symbol, req.OrderID, true)
		case "bybit":
			if bybit == nil {
				err = errors.New("bybit_disabled")
				break
			}
			out, err = bybit.orderStatus(req.Symbol, req.OrderID, true)
		case "htx":
			if htx == nil {
				err = errors.New("htx_disabled")
				break
			}
			out, err = htx.spotOrderStatus(req.OrderID)
		default:
			err = errors.New("unsupported exchange")
		}
		if err != nil {
			_ = json.NewEncoder(w).Encode(response{Success: false, Message: err.Error()})
			return
		}
		_ = json.NewEncoder(w).Encode(out)
	})
	http.HandleFunc("/balances", func(w http.ResponseWriter, r *http.Request) {
		out := map[string]any{}
		if exchanges["okx"] {
			okxBal, _ := okx.balances()
			out["okx"] = okxBal
		}
		if exchanges["bybit"] && bybit != nil {
			bybitBal, _ := bybit.balances()
			out["bybit"] = bybitBal
		}
		if exchanges["htx"] && htx != nil {
			htxBal, _ := htx.balances()
			out["htx"] = htxBal
		}
		_ = json.NewEncoder(w).Encode(out)
	})
	http.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		var sb strings.Builder
		okx.metrics.mu.Lock()
		for k, v := range okx.metrics.count {
			sb.WriteString(fmt.Sprintf("lowlatency_requests_total{exchange=\"okx\",endpoint=\"%s\"} %d\n", k, v))
		}
		for k, v := range okx.metrics.errors {
			sb.WriteString(fmt.Sprintf("lowlatency_errors_total{exchange=\"okx\",endpoint=\"%s\"} %d\n", k, v))
		}
		for k, v := range okx.metrics.latSum {
			cnt := okx.metrics.latCnt[k]
			sb.WriteString(fmt.Sprintf("lowlatency_latency_ms_sum{exchange=\"okx\",endpoint=\"%s\"} %.2f\n", k, v))
			sb.WriteString(fmt.Sprintf("lowlatency_latency_ms_count{exchange=\"okx\",endpoint=\"%s\"} %d\n", k, cnt))
		}
		okx.metrics.mu.Unlock()
		if exchanges["bybit"] && bybit != nil {
			bybit.metrics.mu.Lock()
			for k, v := range bybit.metrics.count {
				sb.WriteString(fmt.Sprintf("lowlatency_requests_total{exchange=\"bybit\",endpoint=\"%s\"} %d\n", k, v))
			}
			for k, v := range bybit.metrics.errors {
				sb.WriteString(fmt.Sprintf("lowlatency_errors_total{exchange=\"bybit\",endpoint=\"%s\"} %d\n", k, v))
			}
			for k, v := range bybit.metrics.latSum {
				cnt := bybit.metrics.latCnt[k]
				sb.WriteString(fmt.Sprintf("lowlatency_latency_ms_sum{exchange=\"bybit\",endpoint=\"%s\"} %.2f\n", k, v))
				sb.WriteString(fmt.Sprintf("lowlatency_latency_ms_count{exchange=\"bybit\",endpoint=\"%s\"} %d\n", k, cnt))
			}
			bybit.metrics.mu.Unlock()
		}
		if exchanges["htx"] && htx != nil {
			htx.metrics.mu.Lock()
			for k, v := range htx.metrics.count {
				sb.WriteString(fmt.Sprintf("lowlatency_requests_total{exchange=\"htx\",endpoint=\"%s\"} %d\n", k, v))
			}
			for k, v := range htx.metrics.errors {
				sb.WriteString(fmt.Sprintf("lowlatency_errors_total{exchange=\"htx\",endpoint=\"%s\"} %d\n", k, v))
			}
			for k, v := range htx.metrics.latSum {
				cnt := htx.metrics.latCnt[k]
				sb.WriteString(fmt.Sprintf("lowlatency_latency_ms_sum{exchange=\"htx\",endpoint=\"%s\"} %.2f\n", k, v))
				sb.WriteString(fmt.Sprintf("lowlatency_latency_ms_count{exchange=\"htx\",endpoint=\"%s\"} %d\n", k, cnt))
			}
			htx.metrics.mu.Unlock()
		}
		_, _ = w.Write([]byte(sb.String()))
	})
	http.HandleFunc("/book", func(w http.ResponseWriter, r *http.Request) {
		ex := r.URL.Query().Get("exchange")
		sym := r.URL.Query().Get("symbol")
		if ex == "" || sym == "" {
			_ = json.NewEncoder(w).Encode(response{Success: false, Message: "missing params"})
			return
		}
		if snap, ok := books.get(ex, sym); ok {
			_ = json.NewEncoder(w).Encode(snap)
			return
		}
		_ = json.NewEncoder(w).Encode(response{Success: false, Message: "not_found"})
	})

	if exchanges["okx"] {
		go startOKXWS(books)
	}
	if exchanges["bybit"] {
		go startBybitWS(books)
	}
	if exchanges["htx"] {
		go startHTXWS(books)
	}

	log.Printf("lowlatency service listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}

func startOKXWS(books *bookCache) {
	url := "wss://ws.okx.com:8443/ws/v5/public"
	if v := strings.TrimSpace(os.Getenv("OKX_WS_URL")); v != "" {
		url = v
	} else if strings.ToLower(os.Getenv("OKX_TESTNET")) == "true" {
		url = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
	}
	symbols := strings.Split(os.Getenv("OKX_WS_SYMBOLS"), ",")
	if len(symbols) == 0 || (len(symbols) == 1 && strings.TrimSpace(symbols[0]) == "") {
		log.Printf("okx ws: no symbols configured (OKX_WS_SYMBOLS)")
	}
	for {
		conn, _, err := websocket.DefaultDialer.Dial(url, nil)
		if err != nil {
			log.Printf("okx ws: connect error: %v", err)
			time.Sleep(2 * time.Second)
			continue
		}
		log.Printf("okx ws: connected to %s", url)
		for _, sym := range symbols {
			s := strings.TrimSpace(sym)
			if s == "" {
				continue
			}
			inst := strings.ReplaceAll(s, "USDT", "-USDT-SWAP")
			sub := map[string]any{
				"op": "subscribe",
				"args": []map[string]string{
					{"channel": "books5", "instId": inst},
				},
			}
			_ = conn.WriteJSON(sub)
			log.Printf("okx ws: subscribed %s", inst)
		}
		for {
			var msg map[string]any
			if err := conn.ReadJSON(&msg); err != nil {
				log.Printf("okx ws: read error: %v", err)
				_ = conn.Close()
				break
			}
			data, ok := msg["data"].([]any)
			if !ok || len(data) == 0 {
				continue
			}
			row := data[0].(map[string]any)
			bids, _ := row["bids"].([]any)
			asks, _ := row["asks"].([]any)
			if len(bids) == 0 || len(asks) == 0 {
				continue
			}
			b0 := bids[0].([]any)
			a0 := asks[0].([]any)
			bid, _ := strconv.ParseFloat(fmt.Sprintf("%v", b0[0]), 64)
			ask, _ := strconv.ParseFloat(fmt.Sprintf("%v", a0[0]), 64)
			inst := fmt.Sprintf("%v", row["instId"])
			sym := strings.ReplaceAll(strings.ReplaceAll(inst, "-USDT-SWAP", "USDT"), "-", "")
			books.set("okx", sym, bid, ask)
		}
	}
}

func startBybitWS(books *bookCache) {
	url := "wss://stream.bybit.com/v5/public/linear"
	if v := strings.TrimSpace(os.Getenv("BYBIT_WS_URL")); v != "" {
		url = v
	} else if strings.ToLower(os.Getenv("BYBIT_TESTNET")) == "true" {
		url = "wss://stream-testnet.bybit.com/v5/public/linear"
	}
	symbols := strings.Split(os.Getenv("BYBIT_WS_SYMBOLS"), ",")
	if len(symbols) == 0 || (len(symbols) == 1 && strings.TrimSpace(symbols[0]) == "") {
		log.Printf("bybit ws: no symbols configured (BYBIT_WS_SYMBOLS)")
	}
	for {
		conn, _, err := websocket.DefaultDialer.Dial(url, nil)
		if err != nil {
			log.Printf("bybit ws: connect error: %v", err)
			time.Sleep(2 * time.Second)
			continue
		}
		log.Printf("bybit ws: connected to %s", url)
		topics := []string{}
		for _, sym := range symbols {
			s := strings.TrimSpace(sym)
			if s == "" {
				continue
			}
			topics = append(topics, "orderbook.1."+s)
		}
		if len(topics) > 0 {
			sub := map[string]any{
				"op":   "subscribe",
				"args": topics,
			}
			_ = conn.WriteJSON(sub)
			log.Printf("bybit ws: subscribed %d topics", len(topics))
		}
		for {
			var msg map[string]any
			if err := conn.ReadJSON(&msg); err != nil {
				log.Printf("bybit ws: read error: %v", err)
				_ = conn.Close()
				break
			}
			data, ok := msg["data"].(map[string]any)
			if !ok {
				continue
			}
			sym, _ := data["s"].(string)
			b, _ := data["b"].([]any)
			a, _ := data["a"].([]any)
			if len(b) == 0 || len(a) == 0 {
				continue
			}
			b0 := b[0].([]any)
			a0 := a[0].([]any)
			bid, _ := strconv.ParseFloat(fmt.Sprintf("%v", b0[0]), 64)
			ask, _ := strconv.ParseFloat(fmt.Sprintf("%v", a0[0]), 64)
			if sym != "" {
				books.set("bybit", sym, bid, ask)
			}
		}
	}
}

func normalizeResponse(exchange string, raw map[string]any) map[string]any {
	resp := map[string]any{
		"success": false,
		"message": "",
		"exchange": exchange,
		"raw":      raw,
	}
	if exchange == "okx" {
		if code, ok := raw["code"].(string); ok && code == "0" {
			resp["success"] = true
		} else {
			resp["message"] = fmt.Sprintf("%v", raw["msg"])
		}
		if data, ok := raw["data"].([]any); ok && len(data) > 0 {
			if row, ok := data[0].(map[string]any); ok {
				resp["order_id"] = fmt.Sprintf("%v", row["ordId"])
			}
		}
	}
	if exchange == "bybit" {
		if code, ok := raw["retCode"].(float64); ok && int(code) == 0 {
			resp["success"] = true
		} else {
			resp["message"] = fmt.Sprintf("%v", raw["retMsg"])
		}
		if result, ok := raw["result"].(map[string]any); ok {
			resp["order_id"] = fmt.Sprintf("%v", result["orderId"])
		}
	}
	if exchange == "htx" {
		if status, ok := raw["status"].(string); ok && status == "ok" {
			resp["success"] = true
		} else {
			resp["message"] = fmt.Sprintf("%v", raw["err-msg"])
		}
		if data, ok := raw["data"].(map[string]any); ok {
			resp["order_id"] = fmt.Sprintf("%v", data["order_id"])
		} else if dataStr, ok := raw["data"].(string); ok {
			resp["order_id"] = dataStr
		} else if oid, ok := raw["order_id"]; ok {
			resp["order_id"] = fmt.Sprintf("%v", oid)
		}
	}
	return resp
}

func envInt(key string, def int) int {
	val := strings.TrimSpace(os.Getenv(key))
	if val == "" {
		return def
	}
	i, err := strconv.Atoi(val)
	if err != nil {
		return def
	}
	return i
}

func routeExchange(books *bookCache, side, symbol string) string {
	side = strings.ToLower(side)
	bestEx := ""
	bestPx := 0.0
	for _, ex := range []string{"okx", "bybit", "htx"} {
		snap, ok := books.get(ex, symbol)
		if !ok {
			continue
		}
		if side == "buy" {
			if bestEx == "" || snap.Ask < bestPx || bestPx == 0 {
				bestEx = ex
				bestPx = snap.Ask
			}
		} else {
			if bestEx == "" || snap.Bid > bestPx {
				bestEx = ex
				bestPx = snap.Bid
			}
		}
	}
	return bestEx
}

func loadOKXInstruments(inst *instrumentCache) {
	base := "https://www.okx.com"
	if v := strings.TrimSpace(os.Getenv("OKX_BASE_URL")); v != "" {
		base = v
	}
	urls := []string{
		base + "/api/v5/public/instruments?instType=SWAP",
		base + "/api/v5/public/instruments?instType=SPOT",
	}
	for _, u := range urls {
		resp, err := http.Get(u)
		if err != nil {
			continue
		}
		var out map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&out)
		_ = resp.Body.Close()
		data, ok := out["data"].([]any)
		if !ok {
			continue
		}
		for _, row := range data {
			r, ok := row.(map[string]any)
			if !ok {
				continue
			}
			instId := fmt.Sprintf("%v", r["instId"])
			sym := strings.ReplaceAll(strings.ReplaceAll(instId, "-USDT-SWAP", "USDT"), "-", "")
			tick, _ := strconv.ParseFloat(fmt.Sprintf("%v", r["tickSz"]), 64)
			minSz, _ := strconv.ParseFloat(fmt.Sprintf("%v", r["minSz"]), 64)
			if sym != "" {
				inst.set("okx", sym, tick, minSz)
			}
		}
	}
}

func loadBybitInstruments(inst *instrumentCache) {
	base := "https://api.bybit.com"
	if strings.ToLower(os.Getenv("BYBIT_TESTNET")) == "true" {
		base = "https://api-testnet.bybit.com"
	}
	urls := []string{
		base + "/v5/market/instruments-info?category=linear",
		base + "/v5/market/instruments-info?category=spot",
	}
	for _, u := range urls {
		resp, err := http.Get(u)
		if err != nil {
			continue
		}
		var out map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&out)
		_ = resp.Body.Close()
		result, ok := out["result"].(map[string]any)
		if !ok {
			continue
		}
		list, ok := result["list"].([]any)
		if !ok {
			continue
		}
		for _, row := range list {
			r, ok := row.(map[string]any)
			if !ok {
				continue
			}
			sym := fmt.Sprintf("%v", r["symbol"])
			priceFilter, _ := r["priceFilter"].(map[string]any)
			lotFilter, _ := r["lotSizeFilter"].(map[string]any)
			tick, _ := strconv.ParseFloat(fmt.Sprintf("%v", priceFilter["tickSize"]), 64)
			minSz, _ := strconv.ParseFloat(fmt.Sprintf("%v", lotFilter["minOrderQty"]), 64)
			if sym != "" {
				inst.set("bybit", sym, tick, minSz)
			}
		}
	}
}

func loadHTXInstruments(inst *instrumentCache) {
	base := "https://api.hbdm.com"
	url := base + "/linear-swap-api/v1/swap_contract_info"
	resp, err := http.Get(url)
	if err != nil {
		return
	}
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	_ = resp.Body.Close()
	data, ok := out["data"].([]any)
	if !ok {
		return
	}
	for _, row := range data {
		r, ok := row.(map[string]any)
		if !ok {
			continue
		}
		cc := fmt.Sprintf("%v", r["contract_code"])
		sym := strings.ReplaceAll(cc, "-", "")
		tick, _ := strconv.ParseFloat(fmt.Sprintf("%v", r["price_tick"]), 64)
		if sym != "" {
			inst.set("htx", sym, tick, 1.0)
		}
	}
}

func startHTXWS(books *bookCache) {
	url := "wss://api.hbdm.com/linear-swap-ws"
	if v := strings.TrimSpace(os.Getenv("HTX_WS_URL")); v != "" {
		url = v
	}
	symbols := strings.Split(os.Getenv("HTX_WS_SYMBOLS"), ",")
	if len(symbols) == 0 || (len(symbols) == 1 && strings.TrimSpace(symbols[0]) == "") {
		log.Printf("htx ws: no symbols configured (HTX_WS_SYMBOLS)")
	}
	for {
		conn, _, err := websocket.DefaultDialer.Dial(url, nil)
		if err != nil {
			log.Printf("htx ws: connect error: %v", err)
			time.Sleep(2 * time.Second)
			continue
		}
		log.Printf("htx ws: connected to %s", url)
		for _, sym := range symbols {
			s := strings.TrimSpace(sym)
			if s == "" {
				continue
			}
			cc := usdtToHTX(s)
			sub := map[string]any{
				"sub": fmt.Sprintf("market.%s.depth.step0", cc),
				"id":  fmt.Sprintf("htx_depth_%s", cc),
			}
			_ = conn.WriteJSON(sub)
			log.Printf("htx ws: subscribed %s", cc)
		}
		for {
			_, raw, err := conn.ReadMessage()
			if err != nil {
				log.Printf("htx ws: read error: %v", err)
				_ = conn.Close()
				break
			}
			msg, err := decodeHTXMessage(raw)
			if err != nil {
				continue
			}
			if ping, ok := msg["ping"]; ok {
				_ = conn.WriteJSON(map[string]any{"pong": ping})
				continue
			}
			ch, _ := msg["ch"].(string)
			if !strings.Contains(ch, "depth") {
				continue
			}
			tick, ok := msg["tick"].(map[string]any)
			if !ok {
				continue
			}
			bids, _ := tick["bids"].([]any)
			asks, _ := tick["asks"].([]any)
			if len(bids) == 0 || len(asks) == 0 {
				continue
			}
			b0 := bids[0].([]any)
			a0 := asks[0].([]any)
			bid, _ := strconv.ParseFloat(fmt.Sprintf("%v", b0[0]), 64)
			ask, _ := strconv.ParseFloat(fmt.Sprintf("%v", a0[0]), 64)
			parts := strings.Split(ch, ".")
			if len(parts) >= 2 {
				sym := strings.ReplaceAll(parts[1], "-", "")
				books.set("htx", sym, bid, ask)
			}
		}
	}
}

func decodeHTXMessage(raw []byte) (map[string]any, error) {
	// HTX sends gzip-compressed payloads
	gr, err := gzip.NewReader(bytes.NewReader(raw))
	if err != nil {
		// try plain JSON
		var out map[string]any
		if err := json.Unmarshal(raw, &out); err != nil {
			return nil, err
		}
		return out, nil
	}
	defer gr.Close()
	var buf bytes.Buffer
	if _, err := buf.ReadFrom(gr); err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(buf.Bytes(), &out); err != nil {
		return nil, err
	}
	return out, nil
}

func usdtToHTX(symbol string) string {
	if strings.Contains(symbol, "-") {
		return strings.ToUpper(symbol)
	}
	if strings.HasSuffix(strings.ToUpper(symbol), "USDT") {
		base := strings.ToUpper(symbol[:len(symbol)-4])
		return base + "-USDT"
	}
	return strings.ToUpper(symbol)
}

func enabledExchanges() map[string]bool {
	raw := strings.TrimSpace(os.Getenv("EXCHANGES"))
	if raw == "" {
		return map[string]bool{"okx": true, "bybit": true}
	}
	out := map[string]bool{}
	for _, item := range strings.Split(raw, ",") {
		ex := strings.ToLower(strings.TrimSpace(item))
		if ex != "" {
			out[ex] = true
		}
	}
	return out
}
