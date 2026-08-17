import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../services/api_service.dart';
import 'taps_screen.dart';
import 'kegs_screen.dart';
import 'stock_screen.dart';
import 'setup_screen.dart';

class HomeScreen extends StatefulWidget {
  final String serverUrl;

  const HomeScreen({super.key, required this.serverUrl});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _selectedIndex = 0;
  late final ApiService _api;

  @override
  void initState() {
    super.initState();
    _api = ApiService(widget.serverUrl);
  }

  Future<void> _disconnect() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('server_url');
    if (!mounted) return;
    Navigator.of(context).pushReplacement(
      MaterialPageRoute(builder: (_) => const SetupScreen()),
    );
  }

  @override
  Widget build(BuildContext context) {
    final screens = [
      TapsScreen(api: _api),
      KegsScreen(api: _api),
      StockScreen(api: _api),
    ];

    return Scaffold(
      appBar: AppBar(
        title: const Text('BarTender'),
        centerTitle: true,
        actions: [
          IconButton(
            icon: const Icon(Icons.link_off),
            tooltip: 'Disconnect',
            onPressed: _disconnect,
          ),
        ],
      ),
      body: screens[_selectedIndex],
      bottomNavigationBar: NavigationBar(
        selectedIndex: _selectedIndex,
        onDestinationSelected: (index) => setState(() => _selectedIndex = index),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.local_bar), label: 'Taps'),
          NavigationDestination(icon: Icon(Icons.sports_bar), label: 'Kegs'),
          NavigationDestination(icon: Icon(Icons.inventory_2), label: 'Stock'),
        ],
      ),
    );
  }
}
