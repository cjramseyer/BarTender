import 'dart:convert';
import 'package:http/http.dart' as http;

import '../models/keg.dart';
import '../models/stock_item.dart';
import '../models/tap.dart';

class ApiService {
  final String baseUrl;
  String? token;

  ApiService(this.baseUrl, {this.token});

  Uri _uri(String path) {
    final base = baseUrl.endsWith('/')
        ? baseUrl.substring(0, baseUrl.length - 1)
        : baseUrl;
    return Uri.parse('$base$path');
  }

  Map<String, String> get _headers => {
        'Accept': 'application/json',
        if (token != null && token!.isNotEmpty) 'Authorization': 'Bearer $token',
      };

  Future<String> login({required String userId, required String pin}) async {
    final response = await http
        .post(
          _uri('/api/mobile/login'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({'user_id': userId, 'pin': pin}),
        )
        .timeout(const Duration(seconds: 10));
    _checkStatus(response);
    final body = jsonDecode(response.body) as Map<String, dynamic>;
    token = body['token'] as String;
    return token!;
  }

  Future<List<Tap>> fetchTaps() async {
    final response =
        await http.get(_uri('/api/taps'), headers: _headers).timeout(const Duration(seconds: 10));
    _checkStatus(response);
    final list = json.decode(response.body) as List<dynamic>;
    return list.map((e) => Tap.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<List<Keg>> fetchKegs() async {
    final response =
        await http.get(_uri('/api/kegs'), headers: _headers).timeout(const Duration(seconds: 10));
    _checkStatus(response);
    final list = json.decode(response.body) as List<dynamic>;
    return list.map((e) => Keg.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<List<StockItem>> fetchStock() async {
    final response =
        await http.get(_uri('/api/stock'), headers: _headers).timeout(const Duration(seconds: 10));
    _checkStatus(response);
    final list = json.decode(response.body) as List<dynamic>;
    return list
        .map((e) => StockItem.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<void> pourTap(
      {required int tapId,
      required double amount,
      required String unit}) async {
    final response = await http
        .post(
          _uri('/api/taps/$tapId/pour'),
          headers: {'Content-Type': 'application/json', ..._headers},
          body: jsonEncode({'amount': amount, 'unit': unit}),
        )
        .timeout(const Duration(seconds: 10));
    _checkStatus(response);
  }

  void _checkStatus(http.Response response) {
    if (response.statusCode < 200 || response.statusCode >= 300) {
      String message = 'Server returned ${response.statusCode}';
      try {
        final body = jsonDecode(response.body);
        if (body is Map<String, dynamic> && body['error'] is String) {
          message = body['error'] as String;
        }
      } catch (_) {
        // Preserve the HTTP status when the response is not JSON.
      }
      throw Exception(message);
    }
  }
}
