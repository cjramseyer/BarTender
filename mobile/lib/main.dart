import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'screens/home_screen.dart';
import 'screens/login_screen.dart';
import 'screens/setup_screen.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final prefs = await SharedPreferences.getInstance();
  final serverUrl = prefs.getString('server_url') ?? '';
  final token = await const FlutterSecureStorage().read(key: 'mobile_token') ?? '';
  runApp(BarTenderApp(initialUrl: serverUrl, initialToken: token));
}

class BarTenderApp extends StatelessWidget {
  final String initialUrl;
  final String initialToken;

  const BarTenderApp({super.key, required this.initialUrl, this.initialToken = ''});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'BarTender',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.amber),
        useMaterial3: true,
      ),
      home: initialUrl.isNotEmpty
          ? (initialToken.isNotEmpty
            ? HomeScreen(serverUrl: initialUrl, token: initialToken)
            : LoginScreen(serverUrl: initialUrl))
          : const SetupScreen(),
    );
  }
}
