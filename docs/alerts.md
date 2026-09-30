# Alert và runbook

Các alert dưới đây dựa trên triệu chứng người dùng và guardrails tại `config/slo.yaml`. Kênh Slack và owner cần được ánh xạ sang workspace/người trực thực tế trước khi triển khai production.

## Alert 1

Name: `HighLatencyP95`.

Name: `HighLatencyP95`.

- Severity: `warning`; duy trì `5m`.
- Kênh: Slack `#k4-l3b-alerts`; owner: `student-repo-owner`.
- SLI/SLO: `response_sent.latency_ms`; cảnh báo khi P95 > 3000 ms trong 5 phút.
- Ảnh hưởng: người dùng phải chờ lâu hơn để nhận câu trả lời.
- Kiểm tra:
  1. Xác nhận P95/P99 và time range trên dashboard latency.
  2. Lọc `data/logs.jsonl` theo khoảng thời gian, chọn `response_sent` có latency cao và ghi `correlation_id`.
  3. Mở trace cùng correlation ID, so sánh duration của retrieval và generation.
- Mitigation: rollback thay đổi gần nhất ở retrieval/prompt nếu trace chứng minh liên quan; trong lab, chỉ tắt incident đã inject sau khi đã thu đủ evidence.
- Theo dõi khôi phục: P95 trở lại ngưỡng SLO trong ít nhất 5 phút.

## Alert 2

Name: `ElevatedRequestErrorRate`.

Name: `ElevatedRequestErrorRate`.

- Severity: `critical`; duy trì `5m`.
- Kênh: Slack `#k4-l3b-alerts`; owner: `student-repo-owner`.
- SLI/guardrail: tỷ lệ request lỗi; cảnh báo khi error rate > 2% trong 5 phút.
- Ảnh hưởng: request không trả được câu trả lời thành công.
- Kiểm tra:
  1. Xác nhận error rate và error breakdown trong dashboard.
  2. Lọc log `request_failed` theo time range, ghi `error_type` và `correlation_id`.
  3. Mở trace tương ứng, xác định observation đầu tiên báo lỗi.
- Mitigation: rollback cấu hình/dependency vừa triển khai; bật fallback đã kiểm thử nếu backend phụ thuộc đang lỗi.
- Theo dõi khôi phục: error rate dưới 2% và không còn nhóm lỗi mới trong 5 phút.

## Alert 3

Name: `LowRetrievalSuccess`.

Name: `LowRetrievalSuccess`.

- Severity: `warning`; duy trì `5m`.
- Kênh: Slack `#k4-l3b-alerts`; owner: `student-repo-owner`.
- SLI/guardrail: retrieval success rate; cảnh báo khi thấp hơn 90% trong 5 phút.
- Ảnh hưởng: câu trả lời có thể thiếu ngữ cảnh hoặc dùng fallback tổng quát.
- Kiểm tra:
  1. Xác nhận retrieval success rate và error breakdown trên dashboard.
  2. Lọc log theo `tool_name`/`tool_success`, rồi lấy `correlation_id` của request thất bại.
  3. Mở trace tương ứng và kiểm tra retrieval observation, duration và lỗi.
- Mitigation: khôi phục index/configuration gần nhất đã biết tốt; chuyển sang fallback an toàn nếu retrieval chưa ổn định.
- Theo dõi khôi phục: retrieval success rate đạt ít nhất 90% trong 5 phút.
