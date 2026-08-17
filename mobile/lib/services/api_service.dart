import 'dart:convert';
import 'package:http/http.dart' as http;

import '../models/keg.dart';
import '../models/stock_item.dart';
import '../models/tap.dart';

class ApiService {
  final String baseUrl;

  ApiService(this.baseUrl);

  Uri _uri(String path) {
    final base = baseUrl.endsWith('/') ? baseUrl.substring(0, baseUrl.length - 1) : baseUrl;
    return Uri.parse('$base$path');
  }

  Future<List<Tap>> fetchTaps() async {
    final response = await http.get(_uri('/api/taps')).timeout(const Duration(seconds: 10));
    _checkStatus(response);
    final list = json.decode(response.body) as List<dynamic>;
    return list.map((e) => Tap.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<List<Keg>> fetchKegs() async {
    final response = await http.get(_uri('/api/kegs')).timeout(const Duration(seconds: 10));
    _checkStatus(response);
    final list = json.decode(response.body) as List<dynamic>;
    return list.map((e) => Keg.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<List<StockItem>> fetchStock() async {
    final response = await http.get(_uri('/api/stock')).timeout(const Duration(seconds: 10));
    _checkStatus(response);
    final list = json.decode(response.body) as List<dynamic>;
    return list.map((e) => StockItem.fromJson(e as Map<String, dynamic>)).toList();
  }

  void _checkStatus(http.Response response) {
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception('Server returned ${response.statusCode}');
    }
  }
}
