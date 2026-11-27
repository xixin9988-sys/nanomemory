# NanoMemory vector eval — nanomemory-support-v1

- provider: hashing (dim 384)
- cases: 44 — 31 recall, 5 refusal, 13 adversarial (29.5%); K=5
- answer-grounding cases (scored by mem-guard, not here): 7
- **Recall@5: 87.1%**, MRR 0.839
- **Refusal accuracy: 100.0%**, false-answer rate 0.0%
- stale-memory violations: 0
- pass rate: 89.2% (33/37)
- latency mean 1.46 ms, p95 1.93 ms
- lexical cases: 29 (recall 93.1%), semantic cases: 2 (recall 0.0%)

| case | type | query | expected | retrieved | rank | signal | refused | result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| c01 | recall | 我的显示偏好是什么？ | m-dark-mode | — | — | 0.0928 | yes | FAIL |
| c02 | recall | 界面字号和动画设置 | m-dark-mode | m-dark-mode | 1 | 0.3006 | no | PASS |
| c03 | recall | 应该用什么语言给客户发邮件？ | m-language | m-language, m-order-a1001, m-contact-phone, m-vip, m-complaint-1 | 1 | 0.1094 | no | PASS |
| c04 | recall | 客户在哪个时区，什么时候方便联系？ | m-timezone | m-contact-phone, m-timezone, m-address-change, m-vip, m-complaint-1 | 2 | 0.1741 | no | PASS |
| c05 | recall | Order A1001 的运单号是多少？ | m-order-a1001 | m-order-a1001, m-refund-policy | 1 | 0.4059 | no | PASS |
| c06 | recall | A1001 什么时候签收的？ | m-order-a1001 | m-order-a1001, m-refund-policy, m-dark-mode, m-contact-phone, m-product-y2 | 1 | 0.2064 | no | PASS |
| c07 | recall | B2044 从哪个仓库发出？ | m-order-b2044 | m-order-b2044, m-order-a1001 | 1 | 0.2064 | no | PASS |
| c08 | recall | 签收后几天内可以无理由退款？ | m-refund-policy | m-refund-policy, m-product-y2 | 1 | 0.5547 | no | PASS |
| c09 | recall | 哪些商品不支持无理由退款？ | m-refund-policy | m-refund-policy, m-product-y2, m-payment, m-refund-r1002 | 1 | 0.5917 | no | PASS |
| c10 | recall | Refund R1001 现在是什么状态？ | m-refund-r1001 | m-refund-r1001, m-refund-r1002 | 1 | 0.2253 | no | PASS |
| c11 | recall | R1002 为什么被拒绝？ | m-refund-r1002 | m-refund-r1002 | 1 | 0.4173 | no | PASS |
| c12 | recall | 新疆的运费怎么算？ | m-shipping-fee | m-shipping-fee, m-cancel | 1 | 0.2064 | no | PASS |
| c13 | recall | 多少钱可以包邮？ | m-shipping-fee | m-shipping-fee | 1 | 0.1253 | no | PASS |
| c14 | recall | 耳机保修多久？ | m-warranty | m-warranty, m-product-x1, m-refund-policy | 1 | 0.288 | no | PASS |
| c15 | recall | 退货寄到哪里？ | m-return-address | m-product-y2, m-return-address | 2 | 0.1274 | no | PASS |
| c16 | recall | 退款金额很大时谁来审批？ | m-escalation | m-escalation, m-refund-r1002, m-invoice | 1 | 0.2953 | no | PASS |
| c17 | recall | 超过 500 元的退款能自动通过吗？ | m-escalation | m-escalation, m-refund-r1002, m-complaint-1 | 1 | 0.5263 | no | PASS |
| c18 | recall | 客户投诉过什么问题？ | m-complaint-1 | m-complaint-1, m-sla, m-order-a1001, m-contact-phone, m-vip | 1 | 0.2834 | no | PASS |
| c19 | recall | 这位客户是金卡会员吗？ | m-vip | m-vip | 1 | 0.3405 | no | PASS |
| c20 | recall | 发票抬头写什么？ | m-invoice | m-invoice | 1 | 0.2637 | no | PASS |
| c21 | recall | X1 耳机多少钱？ | m-product-x1 | m-product-x1, m-warranty, m-timezone, m-refund-policy | 1 | 0.288 | no | PASS |
| c22 | recall | 双十一满减活动什么时候结束？ | m-promo-101 | m-promo-101, m-shipping-fee, m-return-address, m-address-change, m-contact-phone | 1 | 0.1927 | no | PASS |
| c23 | recall | 可以货到付款吗？ | m-payment | m-payment | 1 | 0.4173 | no | PASS |
| c24 | recall | 发货后还能取消订单吗？ | m-cancel | m-cancel, m-address-change | 1 | 0.3422 | no | PASS |
| c25 | recall | 发货后地址还能改吗？ | m-address-change | m-address-change, m-cancel, m-return-address | 1 | 0.2889 | no | PASS |
| c26 | recall | 你们的响应时间承诺是多少？ | m-sla | m-sla, m-promo-101 | 1 | 0.3591 | no | PASS |
| c27 | recall | 我的蓝牙耳塞有质量问题，怎么办？ | m-warranty | m-sla | — | 0.1154 | no | FAIL |
| c28 | recall | 包裹一直没到，能查一下物流吗？ | m-order-a1001, m-order-b2044 | m-contact-phone | — | 0.1502 | no | FAIL |
| c29 | recall | 这个键盘能退吗？ | m-product-y2 | m-product-y2 | 1 | 0.1253 | no | PASS |
| c30 | recall | 退款多久能到账？ | m-refund-r1001 | m-refund-policy, m-refund-r1002, m-escalation | — | 0.1224 | no | FAIL |
| c31 | grounding | Order C9999 的物流状态？ | — | m-contact-phone, m-order-a1001, m-order-b2044 | — | 0.1365 | no | N/A |
| c32 | refusal | 你们在上海有实体店吗？ | — | — | — | 0.0983 | yes | PASS |
| c33 | stale | 2025 年的退款期限是多久？ | — | m-refund-policy, m-escalation, m-refund-r1002 | — | 0.1869 | no | PASS |
| c34 | refusal | CEO 的邮箱是什么？ | — | — | — | 0.0768 | yes | PASS |
| c35 | refusal | 能不能用比特币付款？ | — | — | — | 0.0964 | yes | PASS |
| c36 | grounding | 商品 Z9 的库存还有多少？ | — | m-product-y2, m-product-x1, m-refund-r1002, m-order-b2044, m-refund-r1001 | — | 0.1238 | no | N/A |
| c37 | recall | 退款 R1001 具体哪天到账？ | m-refund-r1001 | m-refund-r1001, m-refund-policy, m-refund-r1002, m-escalation | 1 | 0.1167 | no | PASS |
| c38 | grounding | 客服电话是多少？ | — | m-contact-phone, m-timezone, m-vip, m-complaint-1 | — | 0.1631 | no | N/A |
| c39 | grounding | 下个月的促销计划是什么？ | — | m-warranty | — | 0.1134 | no | N/A |
| c40 | grounding | 这个客户的收货地址在哪里？ | — | m-address-change, m-return-address, m-order-a1001, m-contact-phone | — | 0.2023 | no | N/A |
| c41 | refusal | Apple Watch 的保修政策是什么？ | — | — | — | 0.0863 | yes | PASS |
| c42 | refusal | 你们在成都有仓库吗？ | — | — | — | 0.0947 | yes | PASS |
| c43 | grounding | 会员积分怎么兑换？ | — | m-vip | — | 0.2178 | no | N/A |
| c44 | grounding | 双十二活动的优惠力度是多少？ | — | m-promo-101, m-complaint-1 | — | 0.1568 | no | N/A |
