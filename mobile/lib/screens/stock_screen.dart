import 'package:flutter/material.dart';

import '../models/stock_item.dart';
import '../services/api_service.dart';

class StockScreen extends StatefulWidget {
  final ApiService api;

  const StockScreen({super.key, required this.api});

  @override
  State<StockScreen> createState() => _StockScreenState();
}

class _StockScreenState extends State<StockScreen> {
  late Future<List<StockItem>> _future;

  @override
  void initState() {
    super.initState();
    _future = widget.api.fetchStock();
  }

  void _refresh() => setState(() => _future = widget.api.fetchStock());

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<List<StockItem>>(
      future: _future,
      builder: (context, snapshot) {
        if (snapshot.connectionState == ConnectionState.waiting) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snapshot.hasError) {
          return Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.error_outline, size: 48, color: Colors.red),
                  const SizedBox(height: 12),
                  Text(snapshot.error.toString(), textAlign: TextAlign.center),
                  const SizedBox(height: 16),
                  FilledButton.icon(onPressed: _refresh, icon: const Icon(Icons.refresh), label: const Text('Retry')),
                ],
              ),
            ),
          );
        }

        final items = snapshot.requireData;

        if (items.isEmpty) {
          return Center(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.inventory_2, size: 48, color: Colors.grey),
                const SizedBox(height: 12),
                const Text('No stock items found', style: TextStyle(color: Colors.grey)),
                const SizedBox(height: 16),
                OutlinedButton.icon(onPressed: _refresh, icon: const Icon(Icons.refresh), label: const Text('Refresh')),
              ],
            ),
          );
        }

        // Group items by category
        final grouped = <String, List<StockItem>>{};
        for (final item in items) {
          final cat = item.category.isNotEmpty ? item.category : 'Uncategorized';
          grouped.putIfAbsent(cat, () => []).add(item);
        }
        final categories = grouped.keys.toList()..sort();

        return RefreshIndicator(
          onRefresh: () async => _refresh(),
          child: ListView.builder(
            padding: const EdgeInsets.all(12),
            itemCount: categories.length,
            itemBuilder: (context, catIndex) {
              final category = categories[catIndex];
              final categoryItems = grouped[category]!;
              return Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (catIndex > 0) const SizedBox(height: 8),
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 6, horizontal: 4),
                    child: Text(
                      category,
                      style: Theme.of(context).textTheme.labelLarge?.copyWith(
                            color: Theme.of(context).colorScheme.primary,
                            fontWeight: FontWeight.bold,
                          ),
                    ),
                  ),
                  Card(
                    margin: EdgeInsets.zero,
                    child: Column(
                      children: [
                        for (int i = 0; i < categoryItems.length; i++) ...[
                          if (i > 0) const Divider(height: 1, indent: 16),
                          ListTile(
                            dense: true,
                            title: Text(categoryItems[i].name),
                            trailing: Text(
                              '${categoryItems[i].quantity}${categoryItems[i].unit.isNotEmpty ? " ${categoryItems[i].unit}" : ""}',
                              style: Theme.of(context).textTheme.bodyMedium?.copyWith(fontWeight: FontWeight.w500),
                            ),
                            subtitle: categoryItems[i].notes.isNotEmpty ? Text(categoryItems[i].notes) : null,
                          ),
                        ],
                      ],
                    ),
                  ),
                ],
              );
            },
          ),
        );
      },
    );
  }
}
